"""
Control the Light Crafter 6500DLP evaluation module, or other DMD's relying on the DLPC900 controller over USB.
The code is based around the dlp6500 class, which builds the command packets to be sent to the DMD.
Currently, this code only supports Windows. Extensions to Linux can be accomplished by implementing two functions,
_send_raw_packet() and _get_device(). This would likely also require importing a Linux compatible HID module.

Although Texas Instruments has an SDK for this evaluation module (http://www.ti.com/tool/DLP-ALC-LIGHTCRAFTER-SDK),
it is not very well documented, and we had difficulty building it. Further, it is intended to produce a static library
which cannot be used with e.g. python and the ctypes library as a dll could be.

This DMD control code was originally based on refactoring https://github.com/mazurenko/Lightcrafter6500DMDControl.
The combine_patterns() function was inspired by https://github.com/csi-dcsc/Pycrafter6500.
"""
from collections.abc import Sequence
from typing import Union, Optional
import sys
import time
from struct import pack, unpack
import numpy as np
from copy import deepcopy
import datetime
from argparse import ArgumentParser
# for dealing with configuration files
import json
import zarr
from warnings import warn
from pathlib import Path
from numcodecs import packbits

try:
    import pywinusb.hid as pyhid
except ImportError:
    pyhid = None
    warn("pywinusb could not be imported")


##############################################
# compress DMD pattern data
##############################################
def combine_patterns(patterns: np.ndarray,
                     bit_depth: int = 1):
    """
    Given a series of binary patterns, combine these into 24 bit RGB images to send to DMD. For binary patterns,
    the DMD supports sending a group of up to 24 patterns as an RGB image, with each bit of the 24 bit
    RGB values giving the pattern for one image.

    :param patterns: nimgs x ny x nx array of uint8
    :param bit_depth: 1
    :return combined_patterns:
    """

    if bit_depth != 1:
        raise NotImplementedError('not implemented')

    if not np.all(np.logical_or(patterns == 0, patterns == 1)):
        raise ValueError('patterns must be binary')

    combined_patterns = []
    # determine number of compressed images and create them
    n_combined_patterns = int(np.ceil(len(patterns) / 24))
    for num_pat in range(n_combined_patterns):

        combined_pattern_current = np.zeros((3, patterns.shape[1], patterns.shape[2]), dtype=np.uint8)

        for ii in range(np.min([24, len(patterns) - 24*num_pat])):
            # first 8 patterns encoded in B byte of color image, next 8 in G, last 8 in R
            if ii < 8:
                combined_pattern_current[2, :, :] += patterns[ii + 24*num_pat, :, :] * 2**ii
            elif ii >= 8 and ii < 16:
                combined_pattern_current[1, :, :] += patterns[ii + 24*num_pat, :, :] * 2**(ii-8)
            elif ii >= 16 and ii < 24:
                combined_pattern_current[0, :, :] += patterns[ii + 24*num_pat, :, :] * 2**(ii-16)

        combined_patterns.append(combined_pattern_current)

    return combined_patterns


def split_combined_patterns(combined_patterns) -> np.ndarray:
    """
    Split binary patterns which have been combined into a single uint8 RGB image back to separate images.

    :param combined_patterns: 3 x Ny x Nx uint8 array representing up to 24 combined patterns. Actually
      will accept input of arbitrary dimensions as long as first dimension has size 3.
    :return: 24 x Ny x Nx array. This will always have a first dimension of size 24 because the number of
      zero patterns at the end is ambiguous.
    """
    patterns = np.zeros((24,) + combined_patterns.shape[1:], dtype=np.uint8)

    for ii in range(8):
        patterns[ii] = (combined_patterns[2] & 2**ii) >> ii

    for ii in range(8, 16):
        patterns[ii] = (combined_patterns[1] & 2 ** (ii-8)) >> (ii-8)

    for ii in range(16, 24):
        patterns[ii] = (combined_patterns[0] & 2 ** (ii - 16)) >> (ii+8)

    return patterns


def encode_erle(pattern: np.ndarray) -> list:
    """
    Encode a 24bit pattern in enhanced run length encoding (ERLE).

    ERLE is similar to RLE, but now the number of repeats byte is given by either one or two bytes.

    specification:
    ctrl byte 1, ctrl byte 2, ctrl byte 3, description
    0          , 0          , n/a        , end of image
    0          , 1          , n          , copy n pixels from the same position on the previous line
    0          , n>1        , n/a        , n uncompressed RGB pixels follow
    n>1        , n/a        , n/a        , repeat following pixel n times

    :param pattern: uint8 3 x Ny x Nx array of RGB values, or Ny x Nx array
    :return pattern_compressed:
    """

    # pattern must be uint8
    if pattern.dtype != np.uint8:
        raise ValueError('pattern must be of type uint8')

    # if 2D pattern, expand this to RGB with pattern in B layer and RG=0
    if pattern.ndim == 2:
        pattern = np.concatenate((np.zeros((1,) + pattern.shape, dtype=np.uint8),
                                  np.zeros((1,) + pattern.shape, dtype=np.uint8),
                                  np.array(pattern[None, :, :], copy=True)), axis=0)

    if pattern.ndim != 3 and pattern.shape[0] != 3:
        raise ValueError("Image data is wrong shape. Must be 3 x ny x nx, with RGB values in each layer.")

    pattern_compressed = []
    _, ny, nx = pattern.shape

    # todo: not sure if this is allowed to cross row_rgb boundaries? If so, could pattern.ravel() instead of looping
    # todo: don't think above suggestion works, but if last n pixels of above row_rgb are same as first n of this one
    # todo: then with ERLE encoding I can use \x00\x01 Hex(n). But checking this may not be so easy. Right now
    # todo: only implemented if entire rows are the same!
    # todo: erle and rle are different enough probably should split apart more
    # loop over pattern rows
    for ii in range(pattern.shape[1]):
        row_rgb = pattern[:, ii, :]

        # if this row_rgb is the same as the last row_rgb, can communicate this by sending length of row_rgb
        # and then \x00\x01 (copy n pixels from previous line)
        # todo: can also do this for shorter sequences than the entire row_rgb
        if ii > 0 and np.array_equal(row_rgb, pattern[:, ii - 1, :]):
            msb, lsb = erle_len2bytes(nx)
            pattern_compressed += [0x00, 0x01, msb, lsb]
        else:
            # find points along row where pixel value changes
            # for RGB image, change happens when ANY pixel value changes
            value_changed = np.sum(np.abs(np.diff(row_rgb, axis=1)), axis=0) != 0
            # also need to include zero, as this will need to be encoded.
            # add one to index to get position of first new value instead of last old value
            inds_change = np.concatenate((np.array([0]), np.where(value_changed)[0] + 1))

            # get lengths for each repeat, including last one which extends until end of the line
            run_lens = np.concatenate((np.array(inds_change[1:] - inds_change[:-1]),
                                       np.array([nx - inds_change[-1]])))

            # now build compressed list
            for jj, rlen in zip(inds_change, run_lens):
                v = row_rgb[:, jj]
                length_bytes = erle_len2bytes(rlen)
                pattern_compressed += length_bytes + [v[0], v[1], v[2]]

    # bytes indicating image end
    pattern_compressed += [0x00, 0x01, 0x00]

    return pattern_compressed


def encode_rle(pattern: np.ndarray) -> list:
    """
    Compress pattern use run length encoding (RLE)
    row_rgb length encoding (RLE). Information is encoded as number of repeats
    of a given value and values. In RLE the number of repeats is given by a single byte.
    e.g. AAABBCCCCD = 3A2B4C1D
    The DMD uses a '24bit RGB' encoding scheme, meaning four bits represent each piece of information. The first byte
    (i.e. the control byte) gives the length, and the next three give the values for RGB.
    The only exceptions occur when the control byte is 0x00, in this case there are several options. If the next byte
    is 0x00 this indicates 'end of line', if it is 0x01 this indicates 'end of image', and if it is any other number n,
    then this indicates the following 3*n bytes are uncompressed
    i.e. \x00 \x03 \xAB\xCD\xEF \x11\x22\x33 \x44\x55\x66 -> \xAB\xCD\xEF \x11\x22\x33 \x44\x55\x66

    specification:
    ctrl byte 1, color byte, description
    0          , 0         , end of line
    0          , 1         , end of image (required)
    0          , n>=2      , n uncompressed RGB pixels follow
    n>0        , n/a       , repeat following RGB pixel n times

    :param pattern:
    :return pattern_compressed:
    """
    if pattern.dtype != np.uint8:
        raise ValueError('pattern must be of type uint8')

    # if 2D pattern, expand this to RGB with pattern in B layer and RG=0
    if pattern.ndim == 2:
        pattern = np.concatenate((np.zeros((1,) + pattern.shape, dtype=np.uint8),
                                  np.zeros((1,) + pattern.shape, dtype=np.uint8),
                                  np.array(pattern[None, :, :], copy=True)), axis=0)

    if pattern.ndim != 3 and pattern.shape[0] != 3:
        raise ValueError("Image data is wrong shape. Must be 3 x ny x nx, with RGB values in each layer.")

    pattern_compressed = []
    _, ny, nx = pattern.shape

    # loop over pattern rows
    for ii in range(pattern.shape[1]):
        row_rgb = pattern[:, ii, :]

        # if this row_rgb is the same as the last row_rgb, can communicate this by sending length of row_rgb
        # and then \x00\x01 (copy n pixels from previous line)
        # todo: can also do this for shorter sequences than the entire row_rgb
        if ii > 0 and np.array_equal(row_rgb, pattern[:, ii - 1, :]):
            msb, lsb = erle_len2bytes(nx)
            pattern_compressed += [0x00, 0x01, msb, lsb]
        else:

            # find points along row where pixel value changes
            # for RGB image, change happens when ANY pixel value changes
            value_changed = np.sum(np.abs(np.diff(row_rgb, axis=1)), axis=0) != 0
            # also need to include zero, as this will need to be encoded.
            # add one to index to get position of first new value instead of last old value
            inds_change = np.concatenate((np.array([0]), np.where(value_changed)[0] + 1))

            # get lengths for each repeat, including last one which extends until end of the line
            run_lens = np.concatenate((np.array(inds_change[1:] - inds_change[:-1]),
                                       np.array([nx - inds_change[-1]])))

            # now build compressed list
            for jj, rlen in zip(inds_change, run_lens):
                v = row_rgb[:, jj]
                if rlen <= 255:
                    pattern_compressed += [rlen, v[0], v[1], v[2]]
                else:  # if run is longer than one byte, need to break it up

                    counter = 0
                    while counter < rlen:
                        end_pt = np.min([counter + 255, rlen]) - 1
                        current_len = end_pt - counter + 1
                        pattern_compressed += [current_len, v[0], v[1], v[2]]

                        counter = end_pt + 1
            # todo: do I need an end of line character?

    # todo: is this correct for RLE?
    # bytes indicating image end
    pattern_compressed += [0x00]

    return pattern_compressed


def decode_erle(dmd_size,
                pattern_bytes: list):
    """
    Decode pattern from ERLE or RLE.

    :param dmd_size: [ny, nx]
    :param pattern_bytes: list of bytes representing encoded pattern
    :return rgb_pattern:
    """

    ii = 0  # counter tracking position in compressed byte array
    line_no = 0  # counter tracking line number
    line_pos = 0  # counter tracking next position to write in line
    current_line = np.zeros((3, dmd_size[1]), dtype=np.uint8)
    rgb_pattern = np.zeros((3, 0, dmd_size[1]), dtype=np.uint8)
    # todo: maybe should rewrite popping everything to avoid dealing with at least one counter?
    while ii < len(pattern_bytes):

        # reset each new line
        if line_pos == dmd_size[1]:
            rgb_pattern = np.concatenate((rgb_pattern, current_line[:, None, :]), axis=1)
            current_line = np.zeros((3, dmd_size[1]), dtype=np.uint8)
            line_pos = 0
            line_no += 1
        elif line_pos >= dmd_size[1]:
            raise ValueError("While reading line %d, length of line exceeded expected value" % line_no)

        # end of image denoted by single 0x00 byte
        if ii == len(pattern_bytes) - 1:
            if pattern_bytes[ii] == 0:
                break
            else:
                raise ValueError('Image not terminated with 0x00')

        # control byte of zero indicates special response
        if pattern_bytes[ii] == 0:

            # end of line
            if pattern_bytes[ii + 1] == 0:
                ii += 1
                continue

            # copy bytes from previous lines
            elif pattern_bytes[ii + 1] == 1:
                if pattern_bytes[ii + 2] < 128:
                    n_to_copy = pattern_bytes[ii + 2]
                    ii += 3
                else:
                    n_to_copy = erle_bytes2len(pattern_bytes[ii + 2:ii + 4])
                    ii += 4

                # copy bytes from same position in previous line
                current_line[:, line_pos:line_pos + n_to_copy] = \
                    rgb_pattern[:, line_no-1, line_pos:line_pos + n_to_copy]
                line_pos += n_to_copy

            # next n bytes unencoded
            else:
                if pattern_bytes[ii + 1] < 128:
                    n_unencoded = pattern_bytes[ii + 1]
                    ii += 2
                else:
                    n_unencoded = erle_bytes2len(pattern_bytes[ii + 1:ii + 3])
                    ii += 3

                for jj in range(n_unencoded):
                    current_line[0, line_pos + jj] = int(pattern_bytes[ii + 3*jj])
                    current_line[1, line_pos + jj] = int(pattern_bytes[ii + 3*jj + 1])
                    current_line[2, line_pos + jj] = int(pattern_bytes[ii + 3*jj + 2])

                ii += 3 * n_unencoded
                line_pos += n_unencoded

            continue

        # control byte != 0, regular decoding
        # get block len
        if pattern_bytes[ii] < 128:
            block_len = pattern_bytes[ii]
            ii += 1
        else:
            block_len = erle_bytes2len(pattern_bytes[ii:ii + 2])
            ii += 2

        # write values to lists for rgb colors
        current_line[0, line_pos:line_pos + block_len] = np.asarray([pattern_bytes[ii]] * block_len, dtype=np.uint8)
        current_line[1, line_pos:line_pos + block_len] = np.asarray([pattern_bytes[ii + 1]] * block_len, dtype=np.uint8)
        current_line[2, line_pos:line_pos + block_len] = np.asarray([pattern_bytes[ii + 2]] * block_len, dtype=np.uint8)
        ii += 3
        line_pos += block_len

    return rgb_pattern


def erle_len2bytes(length: int) -> list:
    """
    Encode a length between 0-2**15-1 as 1 or 2 bytes for use in erle encoding format.

    Do this in the following way: if length < 128, encode as one byte
    If length > 128, then encode as two bits. Create the least significant byte (LSB) as follows: set the most
    significant bit as 1 (this is a flag indicating two bytes are being used), then use the least signifcant 7 bits
    from length. Construct the most significant byte (MSB) by throwing away the 7 bits already encoded in the LSB.

    i.e.
    lsb = (length & 0x7F) | 0x80
    msb = length >> 7

    :param length: integer 0-(2**15-1)
    :return len_bytes:
    """

    # check input
    if isinstance(length, float):
        if length.is_integer():
            length = int(length)
        else:
            raise TypeError('length must be convertible to integer.')

    if length < 0 or length > 2 ** 15 - 1:
        raise ValueError('length is negative or too large to be encoded.')

    # main function
    if length < 128:
        len_bytes = [length]
    else:
        # i.e. lsb is formed by taking the 7 least significant bits and extending to 8 bits by adding
        # a 1 in the msb position
        lsb = (length & 0x7F) | 0x80
        # second byte obtained by throwing away first 7 bits and keeping what remains
        msb = length >> 7
        len_bytes = [lsb, msb]

    return len_bytes


def erle_bytes2len(byte_list: list) -> int:
    """
    Convert a 1 or 2 byte list in little endian order to length
    :param list byte_list: [byte] or [lsb, msb]
    :return length:
    """

    if len(byte_list) == 1:
        length = byte_list[0]
    else:
        lsb, msb = byte_list
        length = (msb << 7) + (lsb - 0x80)

    return length


##############################################
# firmware configuration
##############################################
def validate_channel_map(cm: dict) -> (bool, str):
    """
    check that channel_map is of the correct format
    :param cm: dictionary defining channels
    :return success, message:
    """
    for ch in list(cm.keys()):
        modes = list(cm[ch].keys())

        if "default" not in modes:
            return False, f"'default' not present in channel '{ch:s}'"

        for m in modes:
            f_inds = cm[ch][m]
            if not isinstance(f_inds, (np.ndarray, list)):
                return False, f"firmware indices wrong type for channel '{ch:s}', mode '{m:s}'"

            if isinstance(f_inds, np.ndarray) and f_inds.ndim != 1:
                return False, f"firmware indices array with wrong dimension, '{ch:s}', mode '{m:s}'"

    return True, "array validated"


def save_config_file(fname: str,
                     pattern_data: Sequence[dict],
                     channel_map: Optional[dict] = None,
                     firmware_patterns: Optional[np.ndarray] = None,
                     hid_path: Optional[str] = None,
                     use_zarr: bool = True):
    """
    Save DMD firmware configuration data to zarr or json file

    :param fname: file name to save
    :param pattern_data: list of dictionary objects, where each dictionary gives information about the corresponding
      firmware pattern. The structure of these dictionaries is arbitrary, to support different types of user defined
      patterns.
    :param channel_map: a dictionary where the top level keys specify a general mode, e.g. "SIM" or "widefield".
      channel_map[mode] is a dictionary with entries corresponding to collections of patterns. For example, mode "SIM"
      might have pattern collections "blue" and "red". channel_map[mode][channels] is an array of firmware
      indices specifying which patterns are displayed using the given mode and channel
      >>> channel_map = {"SIM": {"blue": np.arange(9).astype(int),
      >>>                        "red": np.arange(9, 18).astype(int)
      >>>                        }
      >>>                }
    :param firmware_patterns: 3D array of size npatterns x ny x nx
    :param hid_path: HID device path allowing the user to address a specific DMD
    :param use_zarr: whether to save configuration file as zarr or json
    :return:
    """

    tstamp = datetime.datetime.now().strftime("%Y_%m_%d_%H;%M;%S")

    # ensure no numpy arrays in pattern_data
    pattern_data_list = deepcopy(pattern_data)
    for p in pattern_data_list:
        for k, v in p.items():
            if isinstance(v, np.ndarray):
                p[k] = v.tolist()

    # ensure no numpy arrays in channel map
    channel_map_list = None
    if channel_map is not None:
        valid, error = validate_channel_map(channel_map)
        if not valid:
            raise ValueError(f"channel_map validation failed with error '{error:s}'")

        # numpy arrays are not seriablizable ... so avoid these
        channel_map_list = deepcopy(channel_map)
        for _, current_ch_dict in channel_map_list.items():
            for m, v in current_ch_dict.items():
                if isinstance(v, np.ndarray):
                    current_ch_dict[m] = v.tolist()

    if use_zarr:
        z = zarr.open(fname, "w")

        if firmware_patterns is not None:
            z.array("firmware_patterns",
                    firmware_patterns.astype(bool),
                    compressor=packbits.PackBits(),
                    dtype=bool,
                    chunks=(1, firmware_patterns.shape[-2], firmware_patterns.shape[-1]))

        z.attrs["timestamp"] = tstamp
        z.attrs["hid_path"] = hid_path
        z.attrs["firmware_pattern_data"] = pattern_data_list
        z.attrs["channel_map"] = channel_map_list

    else:
        if firmware_patterns is not None:
            warn("firmware_patterns were provided but json configuration file was selected."
                          " Use zarr instead to save firmware patterns")

        with open(fname, "w") as f:
            json.dump({"timestamp": tstamp,
                       "firmware_pattern_data": pattern_data_list,
                       "channel_map": channel_map_list,
                       "hid_path": hid_path}, f, indent="\t")


def load_config_file(fname: Union[str, Path]):
    """
    Load DMD firmware data from json configuration file

    :param fname: configuration file path
    :return pattern_data, channel_map, firmware_patterns, tstamp:
    """

    fname = Path(fname)

    if fname.suffix == ".json":
        with open(fname, "r") as f:
            data = json.load(f)

        tstamp = data["timestamp"]
        pattern_data = data["firmware_pattern_data"]
        channel_map = data["channel_map"]
        firmware_patterns = None

        try:
            hid_path = data["hid_path"]
        except KeyError:
            hid_path = None

    elif fname.suffix == ".zarr":
        z = zarr.open(fname, "r")
        tstamp = z.attrs["timestamp"]
        pattern_data = z.attrs["firmware_pattern_data"]
        channel_map = z.attrs["channel_map"]

        try:
            hid_path = z.attrs["hid_path"]
        except KeyError:
            hid_path = None

        try:
            firmware_patterns = z["firmware_patterns"]
        except ValueError:
            firmware_patterns = None

    else:
        raise ValueError(f"fname suffix was '{fname.suffix:s}' but must be '.json' or '.zarr'")

    # convert entries to numpy arrays
    for p in pattern_data:
        for k, v in p.items():
            if isinstance(v, list) and len(v) > 1:
                p[k] = np.atleast_1d(v)

    if channel_map is not None:
        # validate channel map
        valid, error = validate_channel_map(channel_map)
        if not valid:
            raise ValueError(f"channel_map validation failed with error '{error:s}'")

        # convert entries to numpy arrays
        for ch, presets in channel_map.items():
            for mode_name, m in presets.items():
                presets[mode_name] = np.atleast_1d(m)

    return pattern_data, channel_map, firmware_patterns, hid_path, tstamp


def get_preset_info(inds: Sequence,
                    pattern_data: Sequence[dict]) -> dict:
    """
    Get useful data from preset

    :param inds: firmware pattern indices
    :param pattern_data: pattern data for each firmware pattern
    :return pd_all: pattern data dictionary. Dictionary keys will be the same those in each element of pattern_data,
      and values will aggregate the information from pattern data
    """

    pd = [pattern_data[ii] for ii in inds]
    pd_all = {}
    for k in pd[0].keys():
        pd_all[k] = [p[k] for p in pd]

    return pd_all

##############################################
# dlp6500 DMD
##############################################


class dlpc900_dmd:
    """
    Base class for communicating with any DMD using the DLPC900 controller, including the DLP6500 and DLP9000.
    OS specific code should only appear in private functions _get_device(), _send_raw_packet(), and __del__()
    """

    width = None  # pixels
    height = None  # pixels
    pitch = None  # um
    dual_controller = None

    # these used internally
    _dmd = None
    _response = []
    # USB packet length not including report_id_byte
    _packet_length_bytes = 64

    max_lut_index = 511
    min_time_us = 105
    _max_cmd_payload = 504

    dmd_type_code = {0: "unknown",
                     1: "DLP6500",
                     2: "DLP9000",
                     3: "DLP670S",
                     4: "DLP500YX",
                     5: "DLP5500"
                     }

    pattern_modes = {'video': 0x00,
                     'pre-stored': 0x01,
                     'video-pattern': 0x02,
                     'on-the-fly': 0x03
                     }

    compression_modes = {'none': 0x00,
                         'rle': 0x01,
                         'erle': 0x02
                         }

    # tried to match with the TI GUI names where possible
    # see TI "DLPC900 Programmer's Guide", dlpu018.pdf, appendix A for reference
    # available at http://www.ti.com/product/DLPC900/technicaldocuments
    command_dict = {'Read_Error_Code': 0x0100,
                    'Read_Error_Description': 0x0101,
                    'Get_Hardware_Status': 0x1A0A,
                    'Get_System_Status': 0x1A0B,
                    'Get_Main_Status': 0x1A0C,
                    'Get_Firmware_Version': 0x0205,
                    'Get_Firmware_Type': 0x0206,
                    'Get_Firmware_Batch_File_Name': 0x1A14,
                    'Execute_Firmware_Batch_File': 0x1A15,
                    'Set_Firmware_Batch_Command_Delay_Time': 0x1A16,
                    'PAT_START_STOP': 0x1A24,
                    'DISP_MODE': 0x1A1B,
                    'MBOX_DATA': 0x1A34,
                    'PAT_CONFIG': 0x1A31,
                    'PATMEM_LOAD_INIT_MASTER': 0x1A2A,
                    'PATMEM_LOAD_DATA_MASTER': 0x1A2B,
                    'PATMEM_LOAD_INIT_SECONDARY': 0x1A2C,
                    'PATMEM_LOAD_DATA_SECONDARY': 0x1A2D,
                    'TRIG_OUT1_CTL': 0x1A1D,
                    'TRIG_OUT2_CTL': 0x1A1E,
                    'TRIG_IN1_CTL': 0x1A35,
                    'TRIG_IN2_CTL': 0x1A36,
                    }

    err_dictionary = {0: 'no error',
                      1: 'batch file checksum error',
                      2: 'device failure',
                      3: 'invalid command number',
                      4: 'incompatible controller/dmd',
                      5: 'command not allowed in current mode',
                      6: 'invalid command parameter',
                      7: 'item referred by the parameter is not present',
                      8: 'out of resource (RAM/flash)',
                      9: 'invalid BMP compression type',
                      10: 'pattern bit number out of range',
                      11: 'pattern BMP not present in flash',
                      12: 'pattern dark time is out of range',
                      13: 'signal delay parameter is out of range',
                      14: 'pattern exposure time is out of range',
                      15: 'pattern number is out of range',
                      16: 'invalid pattern definition',
                      17: 'pattern image memory address is out of range',
                      255: 'internal error'
                      }

    status_strs = ['DMD micromirrors are parked',
                   'sequencer is running normally',
                   'video is frozen',
                   'external video source is locked',
                   'port 1 syncs valid',
                   'port 2 syncs valid',
                   'reserved',
                   'reserved'
                   ]

    hw_status_strs = ['internal initialization success',
                      'incompatible controller or DMD',
                      'DMD rest controller error',
                      'forced swap error',
                      'slave controller present',
                      'reserved',
                      'sequence abort status error',
                      'sequencer error'
                      ]

    def __init__(self,
                 vendor_id: int = 0x0451,
                 product_id: int = 0xc900,
                 debug: bool = True,
                 firmware_pattern_info: Optional[list] = None,
                 presets: Optional[dict] = None,
                 config_file: Optional[Union[str, Path]] = None,
                 firmware_patterns: Optional[np.ndarray] = None,
                 initialize: bool = True,
                 dmd_index: int = 0,
                 hid_path: Optional[str] = None,
                 platform: Optional[str] = None):
        """
        Get instance of DLP LightCrafter evaluation module (DLP6500 or DLP9000). This is the base class which os
        dependent classes should inherit from. The derived classes only need to implement _get_device and
        _send_raw_packet.

        Note that DMD can be instantiated before being loaded. In this case, use the constructor with initialize=False
        and later call initialize() method with the desired arguments.

        :param vendor_id: vendor id, used to find DMD USB device
        :param product_id: product id, used to find DMD USB device
        :param bool debug: If True, will print output of commands.
        :param firmware_pattern_info:
        :param presets: dictionary of presets
        :param config_file: either provide config file or provide firmware_pattern_info, presets, and firmware_patterns
        :param firmware_patterns: npatterns x ny x nx array of patterns stored in DMD firmware. NOTE, this class
          does not deal with loading or reading patterns from the firmware. Do this with the TI GUI
        :param initialize: whether to connect to the DMD. In certain cases it is convenient to create this object
          before connecting to the DMD, if e.g. we want to pass the DMD to another class, but we don't know what
          DMD index we want yet
        :param dmd_index: If multiple DMD's are attached, choose this one. Indexing starts at zero
        :param hid_path: for more stable identification of a single DMD on multi-DMD systems, provide the hid path.
          This can be obtained from a winusb.hid HIDDevice using the device_path attribute. If an HID path is provided,
          it overrides the dmd_index argument.
        :param platform:
        """

        if config_file is not None and (firmware_pattern_info is not None or
                                        presets is not None or
                                        firmware_patterns is not None):
            raise ValueError("both config_file and either firmware_pattern_info, presets, or firmware_patterns"
                             " were provided. But if config file is provided, these other settings should not be"
                             " set directly.")

        # load configuration file
        if config_file is not None:
            firmware_pattern_info, presets, firmware_patterns, hid_path_config, _ = load_config_file(config_file)

            if hid_path_config is not None:
                if hid_path is not None:
                    warn("hid_path was provided as argument, so value loaded from configuration file will be ignored")
                else:
                    hid_path = hid_path_config

        if firmware_pattern_info is None:
            firmware_pattern_info = []

        if presets is None:
            presets = {}

        # todo: is there a way to read these out from DMD itself?
        if firmware_patterns is not None:
            firmware_patterns = np.array(firmware_patterns)
            self.firmware_indices = np.arange(len(firmware_patterns))
        else:
            self.firmware_indices = None

        # set firmware pattern info
        self.firmware_pattern_info = firmware_pattern_info
        self.presets = presets
        self.firmware_patterns = firmware_patterns

        # on-the-fly patterns
        self.on_the_fly_patterns = None

        self.debug = debug

        # info to find device
        self.vendor_id = vendor_id
        self.product_id = product_id
        self.dmd_index = dmd_index
        self._hid_path = hid_path

        # get platform
        if platform is None:
            self._platform = sys.platform
        else:
            self._platform = platform

        self.initialized = initialize
        if self.initialized:
            self._get_device()

    def __del__(self):
        if self._platform == "win32":
            try:
                self._dmd.close()
            except AttributeError:
                pass  # this will fail if object destroyed before being initialized

    def initialize(self, **kwargs):
        self.__init__(initialize=True, **kwargs)

    # sending and receiving commands, operating system dependence
    def _get_device(self):
        """
        Return handle to DMD. This command can contain OS dependent implementation

        :return:
        """

        if self._platform == "win32":

            if self._hid_path is None:
                devices = pyhid.HidDeviceFilter(vendor_id=self.vendor_id,
                                               product_id=self.product_id).get_devices()
                devices = [d for d in devices if d.product_name == "DLPC900"]

                if len(devices) <= self.dmd_index:
                    raise ValueError(f"Not enough DMD's detected for dmd_index={self.dmd_index:d}."
                                     f"Only {len(devices):d} DMD's were detected.")
                self._dmd = devices[self.dmd_index]
                self._hid_path = self._dmd.device_path
            else:
                self._dmd = pyhid.HidDevice(self._hid_path)

            self._dmd.open()

            # strip off first return byte and add rest to self._response
            self._dmd.set_raw_data_handler(lambda data: self._response.append(data[1:]))
        elif self._platform == "none":
            pass
        else:
            raise NotImplementedError(f"Platform was '{self._platform:s}', "
                                      f"but DMD control is only implemented on 'win32'")

    def _send_raw_packet(self,
                         buffer,
                         listen_for_reply: bool = False,
                         timeout: float = 5):
        """
        Send a single USB packet. This command can contain OS dependent implementations

        :param buffer: list of bytes to send to device
        :param listen_for_reply: whether to listen for a reply
        :param timeout: timeout in seconds
        :return reply: a list of bytes
        """

        # one interesting issue is it seems on linux the report ID byte is stripped
        # by the driver, so we would not need to worry about it here. For windows, we must handle manually.

        if self._platform == "win32":
            # ensure packet is correct length
            assert len(buffer) == self._packet_length_bytes

            report_id_byte = [0x00]

            # clear reply buffer before sending
            self._response = []

            # send
            reports = self._dmd.find_output_reports()
            reports[0].send(report_id_byte + buffer)

            # only wait for a reply if necessary
            if listen_for_reply:
                tstart = time.time()
                while self._response == []:
                    time.sleep(0.1)
                    tnow = time.time()

                    if timeout is not None:
                        if (tnow - tstart) > timeout:
                            print('read command timed out')
                            break

            if self._response != []:
                reply = deepcopy(self._response[0])
            else:
                reply = []

            return reply
        else:
            raise NotImplementedError("DMD control is only implemented on windows")

    def send_raw_command(self,
                         buffer,
                         listen_for_reply: bool = False,
                         timeout: float = 5):
        """
        Send a raw command over USB, possibly including multiple packets. In contrast to send_command,
        this function does not generate the required header data. It deals with splitting
        one command into multiple packets and appropriately padding the supplied buffer.
        This command should not be operating system dependent. All operating system dependence should be
        in _send_raw_packet()

        :param buffer: buffer to send. List of bytes.
        :param listen_for_reply: Boolean. Whether to wait for a reply form USB device
        :param timeout: time to wait for reply, in seconds
        :return: reply: a list of lists of bytes. Each list represents the response for a separate packet.
        """

        reply = []
        # handle sending multiple packets if necessary
        data_counter = 0
        while data_counter < len(buffer):

            # ensure data is correct length
            data_counter_next = data_counter + self._packet_length_bytes
            data_to_send = buffer[data_counter:data_counter_next]

            if len(data_to_send) < self._packet_length_bytes:
                # pad with zeros if necessary
                data_to_send += [0x00] * (self._packet_length_bytes - len(data_to_send))

            packet_reply = self._send_raw_packet(data_to_send, listen_for_reply, timeout)
            reply += packet_reply

            # increment for next packet
            data_counter = data_counter_next

        return reply
 
    def send_command(self,
                     rw_mode: str,
                     reply: bool,
                     command: int,
                     data=(),
                     sequence_byte=0x00):
        """
        Send USB command to DMD

        DMD uses little endian byte order. They also use the convention that, when converting from binary to hex
        the MSB is the rightmost. i.e. \b11000000 = \x03.

        :param rw_mode: 'r' for read, or 'w' for write
        :param reply: boolean
        :param command: two byte integer
        :param data: data to be transmitted. List of integers, where each integer gives a byte
        :param sequence_byte: integer
        :return response_buffer:
        """

        # construct header, 4 bytes long
        # first byte is flag byte
        flagstring = ''
        if rw_mode == 'r':
            flagstring += '1'
        elif rw_mode == 'w':
            flagstring += '0'
        else:
            raise ValueError("flagstring should be 'r' or 'w' but was '%s'" % flagstring)

        # second bit is reply
        if reply:
            flagstring += '1'
        else:
            flagstring += '0'

        # third bit is error bit
        flagstring += '0'
        # fourth and fifth reserved
        flagstring += '00'
        # 6-8 destination
        flagstring += '000'

        # first byte
        flag_byte = int(flagstring, 2)

        # second byte is sequence byte. This is used only to identify responses to given commands.
        # third and fourth are length of payload, respectively LSB and MSB bytes
        len_payload = len(data) + 2
        len_lsb, len_msb = unpack('BB', pack('H', len_payload))

        # get USB command bytes
        cmd_lsb, cmd_msb = unpack('BB', pack('H', command))

        # this does not exactly correspond with what TI calls the header. It is a combination of
        # the report id_byte, the header, and the USB command bytes
        header = [flag_byte, sequence_byte, len_lsb, len_msb, cmd_lsb, cmd_msb]
        buffer = header + list(data)

        # print commands during debugging
        if self.debug:
            # get command name if possible
            # header
            print('header: ' + bin(header[0]), end=' ')
            for ii in range(1, len(header)):
                print("0x%0.2X" % header[ii], end=' ')
            print('')

            # get command name, if possible
            for k, v in self.command_dict.items():
                if v == command:
                    print(k + " (" + hex(command) + ") :", end=' ')
                    break

            # print contents of command
            for ii in range(len(data)):
                print("0x%0.2X" % data[ii], end=' ')
            print('')

        return self.send_raw_command(buffer, reply)

    @staticmethod
    def decode_command(buffer,
                       is_first_packet: bool = True):
        """
        Decode DMD command into constituent pieces

        :param buffer:
        :param is_first_packet:
        :return flag_byte, sequence_byte, data_len, cmd, data:
        """

        if is_first_packet:
            flag_byte = bin(buffer[1])
            sequence_byte = hex(buffer[2])
            len_bytes = pack('B', buffer[4]) + pack('B', buffer[3])
            data_len = unpack('H', len_bytes)[0]
            cmd = pack('B', buffer[6]) + pack('B', buffer[5])
            data = buffer[7:]
        else:
            flag_byte = None
            sequence_byte = None
            len_bytes = None
            data_len = None
            cmd = None
            data = buffer[1:]

        return flag_byte, sequence_byte, data_len, cmd, data

    @staticmethod
    def decode_flag_byte(flag_byte) -> dict:
        """
        Get parameters from flags set in the flag byte

        :param flag_byte:
        :return result:
        """

        errs = [2 ** ii & flag_byte != 0 for ii in range(5, 8)]
        err_names = ['error', 'host requests reply', 'read transaction']
        result = {}
        for e, en in zip(errs, err_names):
            result[en] = e

        return result

    def decode_response(self,
                        buffer) -> dict:
        """
        Parse USB response from DMD into useful info

        :param buffer:
        :return response:
        """

        if buffer == []:
            raise ValueError("buffer was empty")

        flag_byte = buffer[0]
        response = self.decode_flag_byte(flag_byte)

        sequence_byte = buffer[1]

        # len of data
        len_bytes = pack('B', buffer[2]) + pack('B', buffer[3])
        data_len = unpack('<H', len_bytes)[0]

        # data
        data = buffer[4:4 + data_len]

        # all information
        response.update({'sequence byte': sequence_byte, 'data': data})

        return response

    # check DMD info
    def read_error_code(self) -> (str, int):
        """
        Retrieve error code number from last executed command
        """

        # todo: DMD complains about this command...says invalid command number 0x100

        buffer = self.send_command('w', True, self.command_dict["Read_Error_Code"])
        resp = self.decode_response(buffer)
        if len(resp["data"]) > 0:
            err_code = resp['data'][0]
        else:
            err_code = None

        try:
            error_type = self.err_dictionary[err_code]
        except KeyError:
            error_type = 'not defined'

        return error_type, err_code

    def read_error_description(self) -> str:
        """
        Retrieve error code description for the last error.

        When new error messages are written to the DMD buffer, they are written over previous messages.
        If the new error messages is shorter than the previous one, the remaining characters from earlier errors
        will still be in the buffer and may be returned.

        :return err_description:
        """
        buffer = self.send_command('r', True, self.command_dict["Read_Error_Description"])
        resp = self.decode_response(buffer)

        # read until find C style string termination byte, \x00
        err_description = ''
        for ii, d in enumerate(resp['data']):
            if d == 0:
                break

            err_description += chr(d)

        return err_description

    def get_hw_status(self) -> dict:
        """
        Get hardware status of DMD

        :return:
        """
        buffer = self.send_command('r', True, self.command_dict["Get_Hardware_Status"])
        resp = self.decode_response(buffer)

        errs = [(2**ii & resp['data'][0]) != 0 for ii in range(8)]

        result = {}
        for e, en in zip(errs, self.hw_status_strs):
            result[en] = e

        return result

    def get_system_status(self) -> dict:
        """
        Get status of internal memory test

        :return:
        """
        buffer = self.send_command('r',
                                   True,
                                   self.command_dict["Get_System_Status"])
        resp = self.decode_response(buffer)

        return {'internal memory test passed': bool(resp['data'][0])}

    def get_main_status(self) -> dict:
        """
        Get DMD main status

        :return:
        """

        buffer = self.send_command('r',
                                   True,
                                   self.command_dict["Get_Main_Status"])
        resp = self.decode_response(buffer)

        errs = [2 ** ii & resp['data'][0] != 0 for ii in range(8)]

        result = {}
        for e, en in zip(errs, self.status_strs):
            result[en] = e

        return result

    def get_firmware_version(self) -> dict:
        """
        Get firmware version information from DMD

        :return dict:
        """
        buffer = self.send_command('r',
                                   True,
                                   self.command_dict["Get_Firmware_Version"])
        resp = self.decode_response(buffer)

        app_version = resp['data'][0:4]
        app_patch = unpack('<H', b"".join([b.to_bytes(1, 'big') for b in app_version[0:2]]))[0]
        app_minor = app_version[2]
        app_major = app_version[3]
        app_version_str = '%d.%d.%d' % (app_major, app_minor, app_patch)

        api_version = resp['data'][4:8]
        api_patch = unpack('<H', b"".join([b.to_bytes(1, 'big') for b in api_version[0:2]]))[0]
        api_minor = api_version[2]
        api_major = api_version[3]
        api_version_str = '%d.%d.%d' % (api_major, api_minor, api_patch)

        software_config_revision = resp['data'][8:12]
        swc_patch = unpack('<H', b"".join([b.to_bytes(1, 'big') for b in software_config_revision[0:2]]))[0]
        swc_minor = software_config_revision[2]
        swc_major = software_config_revision[3]
        swc_version_str = '%d.%d.%d' % (swc_major, swc_minor, swc_patch)

        sequencer_config_revision = resp['data'][12:16]
        sqc_patch = unpack('<H', b"".join([b.to_bytes(1, 'big') for b in sequencer_config_revision[0:2]]))[0]
        sqc_minor = sequencer_config_revision[2]
        sqc_major = sequencer_config_revision[3]
        sqc_version_str = '%d.%d.%d' % (sqc_major, sqc_minor, sqc_patch)

        result = {'app version': app_version_str,
                  'api version': api_version_str,
                  'software configuration revision': swc_version_str,
                  'sequence configuration revision': sqc_version_str}

        return result

    def get_firmware_type(self) -> dict:
        """
        Get DMD type and firmware tag

        :return dict:
        """
        buffer = self.send_command('r', True, self.command_dict["Get_Firmware_Type"])
        resp = self.decode_response(buffer)

        dmd_type_flag = resp['data'][0]
        try:
            dmd_type = self.dmd_type_code[dmd_type_flag]
        except KeyError:
            raise ValueError(f"Unknown DMD type index {dmd_type_flag:d}. "
                             f"Allowed values are {self.dmd_type_code}")

        # TODO: in principle could receive two packets. handle that case
        firmware_tag = ''
        for d in resp['data'][1:]:
            # terminate on end of string \x00
            if d == 0:
                break
            firmware_tag += chr(d)

        return {'dmd type': dmd_type, 'firmware tag': firmware_tag}

    # trigger setup
    def set_trigger_out(self,
                        trigger_number: int = 1,
                        invert: bool = False,
                        rising_edge_delay_us: int = 0,
                        falling_edge_delay_us: int = 0):
        """
        Set DMD output trigger delays and polarity. Trigger 1 is the "advance frame" trigger and trigger 2 is the
        "enable" trigger

        :param trigger_number:
        :param invert:
        :param rising_edge_delay_us:
        :param falling_edge_delay_us:
        :return response:
        """

        # todo: test this function

        if rising_edge_delay_us < -20 or rising_edge_delay_us > 20e3:
            raise ValueError('rising edge delay must be in range -20 -- 20000us')

        if falling_edge_delay_us < -20 or falling_edge_delay_us > 20e3:
            raise ValueError('falling edge delay must be in range -20 -- 20000us')

        if invert:
            assert rising_edge_delay_us >= falling_edge_delay_us

        # data
        trig_byte = [int(invert)]
        rising_edge_bytes = unpack('BB', pack('<h', rising_edge_delay_us))
        falling_edge_bytes = unpack('BB', pack('<h', falling_edge_delay_us))
        data = trig_byte + list(rising_edge_bytes) + list(falling_edge_bytes)

        if trigger_number == 1:
            resp = self.send_command('w', True, self.command_dict["TRIG_OUT1_CTL"], data)
        elif trigger_number == 2:
            resp = self.send_command('w', True, self.command_dict["TRIG_OUT2_CTL"], data)
        else:
            raise ValueError('trigger_number must be 1 or 2')

        return resp

    def get_trigger_in1(self):
        """
        Query information about trigger 1 ("advance frame" trigger)

        :return delay_us, mode:
        """
        buffer = self.send_command('r', True, self.command_dict["TRIG_IN1_CTL"], [])
        resp = self.decode_response(buffer)
        data = resp['data']
        delay_us, = unpack('<H', pack('B', data[0]) + pack('B', data[1]))
        mode = data[2]

        return delay_us, mode

    def set_trigger_in1(self,
                        delay_us: int = 105,
                        edge_to_advance: str = 'rising'):
        """
        Set delay and pattern advance edge for trigger input 1 ("advance frame" trigger)

        Trigger input 1 is used to advance the pattern displayed on the DMD, provided trigger_in2 is
        in the appropriate state

        :param delay_us:
        :param edge_to_advance: 'rising' or 'falling'
        :return response:
        """

        if delay_us < 104:
            raise ValueError(f'delay time must be {self.min_time_us:.0f}us or longer.')

        # todo: is this supposed to be a signed or unsigned integer?
        delay_byte = list(unpack('BB', pack('<H', delay_us)))

        if edge_to_advance == 'rising':
            advance_byte = [0x00]
        elif edge_to_advance == 'falling':
            advance_byte = [0x01]
        else:
            raise ValueError("edge_to_advance must be 'rising' or 'falling', but was '%s'" % edge_to_advance)

        return self.send_command('w', True, self.command_dict["TRIG_IN1_CTL"], delay_byte + advance_byte)

    def get_trigger_in2(self):
        """
        Query polarity of trigger in 2 ("enable" trigger)

        :return mode:
        """
        buffer = self.send_command('r', True, self.command_dict["TRIG_IN2_CTL"], [])
        resp = self.decode_response(buffer)
        mode = resp['data'][0]
        return mode

    def set_trigger_in2(self,
                        edge_to_start: str = 'rising'):
        """
        Set polarity to start/stop pattern on for input trigger 2 ("enable" trigger)
        Trigger input 2 is used to start or stop the DMD pattern display.

        :param edge_to_start:
        :return response:
        """
        if edge_to_start == 'rising':
            start_byte = [0x00]
        elif edge_to_start == 'falling':
            start_byte = [0x01]
        else:
            raise ValueError("edge_to_start must be 'rising' or 'falling', but was '%s'" % edge_to_start)

        return self.send_command('w', False, self.command_dict["TRIG_IN2_CTL"], start_byte)

    # sequence start stop
    def set_pattern_mode(self,
                         mode: str = 'on-the-fly'):
        """
        Change the DMD display mode

        :param mode: 'video', 'pre-stored', 'video-pattern', or 'on-the-fly'
        :return response:
        """
        if mode not in self.pattern_modes.keys():
            raise ValueError(f"mode was '{mode:s}', but the only supported values are {self.pattern_modes}")

        data = [self.pattern_modes[mode]]

        return self.send_command('w', True, self.command_dict["DISP_MODE"], data)

    def start_stop_sequence(self,
                            cmd: str):
        """
        Start, stop, or pause a pattern sequence. Note that calling "start" will cause whatever state the DMD
        enable trigger is currently in (HIGH or LOW) to be treated as enabled. While calling "stop" will cause
        whatever state the enable trigger is currently in to be treated as disabled.

        :param cmd: string. 'start', 'stop' or 'pause'
        :return response:
        """
        if cmd == 'start':
            data = [0x02]
            seq_byte = 0x08
        elif cmd == 'stop':
            data = [0x00]
            seq_byte = 0x05
        elif cmd == 'pause':
            data = [0x01]
            seq_byte = 0x00  # todo: check this from packet sniffer
        else:
            raise ValueError(f"cmd must be 'start', 'stop', or 'pause', but was '{cmd:s}'")

        return self.send_command('w', False, self.command_dict["PAT_START_STOP"], data, sequence_byte=seq_byte)

    #######################################
    # commands for working batch files in firmware
    #######################################
    def get_fwbatch_name(self,
                         batch_index: int) -> str:
        """
        Return name of batch file stored on firmware at batch_index

        :param batch_index:
        :return bach_name:
        """
        buffer = self.send_command('r', True, self.command_dict["Get_Firmware_Batch_File_Name"], [batch_index])
        resp = self.decode_response(buffer)

        batch_name = ''
        for ii, d in enumerate(resp['data']):
            if d == 0:
                break

            batch_name += chr(d)

        return batch_name

    def execute_fwbatch(self,
                        batch_index: int):
        """
        Execute batch file stored on firmware at index batch_index

        :param batch_index:
        :return response:
        """
        return self.send_command('w', True, self.command_dict["Execute_Firmware_Batch_File"], [batch_index])

    def set_fwbatch_delay(self,
                          delay_ms: int):
        """
        Set delay between batch file commands

        :param delay_ms:
        :return response:
        """
        raise NotImplementedError("this function not yet implemented. testing needed")

        data = unpack('BBBB', pack('<I', delay_ms))
        data = list(data[:3])
        return self.send_command('w', True, self.command_dict["Set_Firmware_Batch_Command_Delay_Time"], data)

    #######################################
    # low-level commands for working with patterns and pattern sequences
    #######################################
    def _pattern_display_lut_configuration(self,
                                           num_patterns: int,
                                           num_repeat: int = 0):
        """
        Controls the execution of patterns stored in the lookup table (LUT). Before executing this command,
        stop the current pattern sequence.

        "PAT_CONFIG" according to GUI

        :param num_patterns: Number of LUT entries, 0-511
        :param num_repeat: number of times to repeat the pattern sequence
        :return response:
        """
        if num_patterns > self.max_lut_index:
            raise ValueError(f"num_patterns must be <= {self.max_lut_index:d} but was {num_patterns:d}")

        num_patterns_bytes = list(unpack('BB', pack('<H', num_patterns)))
        num_repeats_bytes = list(unpack('BBBB', pack('<I', num_repeat)))

        return self.send_command('w',
                                 True,
                                 self.command_dict["PAT_CONFIG"],
                                 data=num_patterns_bytes + num_repeats_bytes)

    def _pattern_display_lut_definition(self,
                                        sequence_position_index: int,
                                        exposure_time_us: int = 105,
                                        dark_time_us: int = 0,
                                        wait_for_trigger: bool = True,
                                        clear_pattern_after_trigger: bool = False,
                                        bit_depth: int = 1,
                                        disable_trig_2: bool = True,
                                        stored_image_index: int = 0,
                                        stored_image_bit_index: int = 0):
        """
        Define parameters for pattern used in on-the-fly mode. This command is listed as "MBOX_DATA"
         in the DLPLightcrafter software GUI.

        Display mode and pattern display LUT configuration must be set before sending pattern LUT definition data.
        These can be set using set_pattern_mode() and pattern_display_lut_configuration() respectively.  If the pattern
        display data input source is set to streaming the image indices do not need to be set.

        When uploading 1 bit image, each set of 24 images are first combined to a single 24 bit RGB image. pattern_index
        refers to which 24 bit RGB image a pattern is in, and pattern_bit_index refers to which bit of that image
        it is stored in.

        :param sequence_position_index:
        :param exposure_time_us:
        :param dark_time_us:
        :param wait_for_trigger:
        :param clear_pattern_after_trigger:
        :param bit_depth: 1, 2, 4, 8
        :param disable_trig_2: (disable "enable" trigger)
        :param stored_image_index:
        :param stored_image_bit_index: index of the RGB image (in DMD memory) storing the given pattern
          this index tells which bit to look at in that image. This should be 0-23
        :return response:
        """

        # assert pattern_index < 256 and pattern_index >= 0

        pattern_index_bytes = list(unpack('BB', pack('<H', sequence_position_index)))
        # actually can only use the first 3 bytes
        exposure_bytes = list(unpack('BBBB', pack('<I', exposure_time_us)))[:-1]

        # next byte contains various different information
        # first bit gives
        misc_byte_str = ''

        if clear_pattern_after_trigger:
            misc_byte_str += '1'
        else:
            misc_byte_str += '0'

        # next three bits give bit depth, integer 1 = 000, ..., 8 = 111
        if bit_depth != 1:
            raise NotImplementedError('bit_depths other than 1 not implemented.')
        misc_byte_str += '000'

        # next 3 give LED's enabled or disabled. Always disabled
        # todo: think usually GUI sends command to 100 for this?
        misc_byte_str += '100'

        if wait_for_trigger:
            misc_byte_str += '1'
        else:
            misc_byte_str += '0'

        misc_byte = [int(misc_byte_str[::-1], 2)]

        dark_time_bytes = list(unpack('BB', pack('<H', dark_time_us))) + [0]
        if disable_trig_2:
            trig2_output_bytes = [0x00]
        else:
            trig2_output_bytes = [0x01]

        # actually bits 0:10
        img_pattern_index_byte = [stored_image_index]
        # todo: how to set this byte?
        # actually only bits 11:15
        # don't understand why, but this is what GUI sets for these...
        # NOTE: can reuse a pattern in the LUT by setting this bit to the same as another
        # in that case would not need to send the PATMEM_LOAD_INIT_MASTER or -TAMEM_LOAD_DATA_MASTER commands
        pattern_bit_index_byte = [8 * stored_image_bit_index]

        data = pattern_index_bytes + exposure_bytes + misc_byte + \
               dark_time_bytes + trig2_output_bytes + img_pattern_index_byte + pattern_bit_index_byte

        return self.send_command('w', True, self.command_dict["MBOX_DATA"], data)

    def _init_pattern_bmp_load(self,
                               pattern_length: int,
                               pattern_index: int,
                               primary_controller: bool = True):
        """
        Initialize pattern BMP load command.

        DMD GUI calls this "PATMEM_LOAD_INIT_MASTER"

        When the initialize pattern BMP load command is issued, the patterns in the flash are not used until the pattern
        mode is disabled by command. This command should be followed by the pattern_bmp_load() command to load images.
        The images should be loaded in reverse order.

        :param pattern_length:
        :param pattern_index:
        :param primary_controller: whether to send command to primary or secondary controller.
          Not all DMD models have a secondary controller.
        :return response:
        """

        # packing and unpacking bytes doesn't do anything...but for consistency...
        index_bin = list(unpack('BB', pack('<H', pattern_index)))
        num_bytes = list(unpack('BBBB', pack('<I', pattern_length)))
        data = index_bin + num_bytes

        if primary_controller:
            cmd = self.command_dict["PATMEM_LOAD_INIT_MASTER"]
        else:
            cmd = self.command_dict["PATMEM_LOAD_INIT_SECONDARY"]

        return self.send_command('w', True, cmd, data=data)

    def _pattern_bmp_load(self,
                          compressed_pattern: list,
                          compression_mode: str,
                          pattern_index: int = 0,
                          primary_controller: bool = True):
        """
        Load DMD pattern data for use in pattern on-the-fly mode. To load all necessary data to DMD correctly,
        invoke this from upload_pattern_sequence()

        The DMD GUI software calls this command "PATMEM_LOAD_DATA_MASTER"

        Some complications to this command: the DMD can only deal with 512 bytes at a time. So including the packet
        header, can only send 512 - len(header) - len_command_data_bytes.
        since the header is 6 bytes and the length of the data is represented using 2 bytes, there are 504 data bytes
        After this, have to send a new command.

        :param compressed_pattern:
        :param compression_mode:
        :param primary_controller: whether to send command to primary or secondary controller.
          Not all DMD models have a secondary controller.
        :return:
        """

        if self.dual_controller:
            width = self.width // 2
        else:
            width = self.width

        # get the header, 48 bytes long
        # Note: taken directly from sniffer of the TI GUI
        signature_bytes = [0x53, 0x70, 0x6C, 0x64]
        width_byte = list(unpack('BB', pack('<H', width)))
        height_byte = list(unpack('BB', pack('<H', self.height)))
        # Number of bytes in encoded image_data
        num_encoded_bytes = list(unpack('BBBB', pack('<I', len(compressed_pattern))))
        reserved_bytes = [0xFF] * 8  # reserved
        bg_color_bytes = [0x00] * 4  # BG color BB, GG, RR, 00

        if compression_mode not in self.compression_modes.keys():
            raise ValueError(f"compression_mode was '{compression_mode:s}', "
                             f"but must be one of {self.compression_modes.keys()}")
        encoding_byte = [self.compression_modes[compression_mode]]

        general_data = signature_bytes + width_byte + height_byte + num_encoded_bytes + \
                       reserved_bytes + bg_color_bytes + [0x01] + encoding_byte + \
                       [0x01] + [0x00] * 2 + [0x01] + [0x00] * 18  # reserved

        data = general_data + compressed_pattern

        # call init before loading pattern
        # todo: check len(data) = len(compressed_pattern) + 48 and replace in command
        buffer = self._init_pattern_bmp_load(len(compressed_pattern) + 48,
                                             pattern_index=pattern_index,
                                             primary_controller=primary_controller)
        resp = self.decode_response(buffer)
        if resp['error']:
            print(self.read_error_description())

        # send pattern
        if primary_controller:
            cmd = self.command_dict["PATMEM_LOAD_DATA_MASTER"]
        else:
            cmd = self.command_dict["PATMEM_LOAD_DATA_SECONDARY"]

        # send multiple commands, each of maximum size 512 bytes including header
        data_index = 0
        command_index = 0
        while data_index < len(data):
            # slice data to get block to send in this command
            data_index_next = np.min([data_index + self._max_cmd_payload, len(data)])
            data_current = data[data_index:data_index_next]

            # len of current data block
            data_len_bytes = list(unpack('BB', pack('<H', len(data_current))))

            # send command
            self.send_command('w', False, cmd, data=data_len_bytes + data_current)

            data_index = data_index_next
            command_index += 1

    def upload_pattern_sequence(self,
                                patterns: np.ndarray,
                                exp_times: Optional[Union[Sequence[int], int]] = None,
                                dark_times: Optional[Union[Sequence[int], int]] = 0,
                                triggered: bool = False,
                                clear_pattern_after_trigger: bool = True,
                                bit_depth: int = 1,
                                num_repeats: int = 0,
                                compression_mode: str = 'erle'):
        """
        Upload on-the-fly pattern sequence to DMD. This command is based on Table 5-3 in the DLP programming manual.
        After loading patterns, the pattern sequence can be configured with set_pattern_sequence(). If you wish to 
        run the patterns sequentially exactly as uploaded, it is not necessary to call set_pattern_sequence(). 

        Note that the DMD behaves differently depending on the state of the trigger input lines when
        a "start" or "stop" command is issued, as it will be at the end of this function. See start_stop_sequence()
        for more details. When a "start" command is
        issued, the DMD will treat whatever state the DMD Enable trigger is in (HIGH or LOW) as enabling the pattern
        sequence. Alternatively, when a stop command is issued, the opposite of the current state of the DMD
        enable trigger will enable the pattern. If you issue several of these commands, the DMD seems to exhibit
        some memory of the immediate previous enable trigger. Specifically, if you change which state
        corresponds to enabled, then the DMD needs to see one falling edge from the
        Advance trigger before it will respond to a rising edge (assuming the advance trigger is set to rising
        edge mode). So it is best practice to keep the advance trigger in the HIGH state when programming the DMD.

        :param patterns: N x Ny x Nx NumPy array of uint8
        :param exp_times: exposure times in us. Either a uint8, or a sequence the same
          length as the number of patterns. Must be >= self.minimum_time_us
        :param dark_times: dark times in us. Either a uint8, or a sequence the same length as the number of patterns
        :param triggered: Whether the DMD should wait for any advance frame trigger to display the next pattern
        :param clear_pattern_after_trigger: If True, clear the DMD pattern after exp_time and display an OFF pattern
          while awaiting the next trigger. If False, after exp_time keep displaying the current pattern.
        :param bit_depth: bit depth of patterns
        :param num_repeats: Number of repeats. 0 means infinite.
        :param compression_mode: 'erle', 'rle', or 'none'
        """
        # #########################
        # check arguments
        # #########################
        if patterns.dtype != np.uint8:
            raise ValueError('patterns must be of dtype uint8')

        if patterns.ndim == 2:
            patterns = np.expand_dims(patterns, axis=0)

        npatterns = len(patterns)

        if exp_times is None:
            exp_times = self.min_time_us

        # if only one exp_times, apply to all patterns
        if not isinstance(exp_times, (list, np.ndarray)):
            exp_times = [exp_times]

        if not all(list(map(lambda t: isinstance(t, int), exp_times))):
            raise ValueError("exp_times must be a list of integers")

        if patterns.shape[0] > 1 and len(exp_times) == 1:
            exp_times = exp_times * patterns.shape[0]

        # if only one dark_times, apply to all patterns
        if isinstance(dark_times, int):
            dark_times = [dark_times]

        if not all(list(map(lambda t: isinstance(t, int), dark_times))):
            raise ValueError("dark_times must be a list of integers")

        if patterns.shape[0] > 1 and len(dark_times) == 1:
            dark_times = dark_times * patterns.shape[0]

        if compression_mode not in self.compression_modes.keys():
            raise ValueError(f"compression mode was '{compression_mode:s}', "
                             f"but must be one of {self.compression_modes.keys()}")

        if compression_mode != "erle":
            raise NotImplementedError("Currently only `erle` compression is implemented")

        if compression_mode == 'none':
            def compression_fn(p): return np.packbits(p.ravel())
        elif compression_mode == 'rle':
            compression_fn = encode_rle
        elif compression_mode == 'erle':
            compression_fn = encode_erle

        # #########################
        # #########################
        # store patterns so we can check what is uploaded later
        self.on_the_fly_patterns = patterns

        # need to issue stop before changing mode, otherwise DMD will sometimes lock up and not be responsive.
        self.start_stop_sequence('stop')

        # set to on-the-fly mode
        buffer = self.set_pattern_mode('on-the-fly')
        resp = self.decode_response(buffer)
        if resp['error']:
            print(self.read_error_description())

        # stop after changing pattern mode, otherwise may throw error
        self.start_stop_sequence('stop')

        # set image parameters for look up table
        # When uploading 1 bit image, each set of 24 images are first combined to a single 24 bit RGB image.
        # pattern_index refers to which 24 bit RGB image a pattern is in, and pattern_bit_index refers to
        # which bit of that image (i.e. in the RGB bytes, it is stored in.
        for ii, (p, et, dt) in enumerate(zip(patterns, exp_times, dark_times)):
            pic_ind, bit_ind = self._index_2pic_bit(ii)
            buffer = self._pattern_display_lut_definition(ii,
                                                          exposure_time_us=et,
                                                          dark_time_us=dt,
                                                          wait_for_trigger=triggered,
                                                          clear_pattern_after_trigger=clear_pattern_after_trigger,
                                                          bit_depth=bit_depth,
                                                          stored_image_index=pic_ind,
                                                          stored_image_bit_index=bit_ind)
            resp = self.decode_response(buffer)
            if resp['error']:
                print(self.read_error_description())

        buffer = self._pattern_display_lut_configuration(npatterns, num_repeats)
        resp = self.decode_response(buffer)
        if resp['error']:
            print(self.read_error_description())

        # can combine images if bit depth = 1
        if bit_depth == 1:
            patterns = combine_patterns(patterns)
        else:
            raise NotImplementedError("Combining multiple images into a 24-bit RGB image is only"
                                      " implemented for bit depth 1.")

        # compress and load images in backwards order
        for ii, dmd_pattern in reversed(list(enumerate(patterns))):
            if self.debug:
                print(f"sending pattern {ii + 1:d}/{len(patterns):d}")

            if self.dual_controller:
                p0, p1 = np.array_split(dmd_pattern, 2, axis=-1)
                cp0 = compression_fn(p0)
                cp1 = compression_fn(p1)
                self._pattern_bmp_load(cp0,
                                       compression_mode,
                                       pattern_index=ii,
                                       primary_controller=True)
                self._pattern_bmp_load(cp1,
                                       compression_mode,
                                       pattern_index=ii,
                                       primary_controller=False)
            else:
                compressed_pattern = compression_fn(dmd_pattern)
                self._pattern_bmp_load(compressed_pattern,
                                       compression_mode,
                                       pattern_index=ii,
                                       primary_controller=True)

        # this command is necessary, otherwise subsequent calls to set_pattern_sequence() will not behave as expected
        buffer = self._pattern_display_lut_configuration(npatterns, num_repeats)
        resp = self.decode_response(buffer)
        if resp['error']:
            print(self.read_error_description())

        self.start_stop_sequence('start')

        if triggered:
            self.start_stop_sequence('stop')


    def set_pattern_sequence(self,
                             pattern_indices: Sequence[int],
                             exp_times: Optional[Union[Sequence[int], int]] = None,
                             dark_times: Union[Sequence[int], int] = 0,
                             triggered: bool = False,
                             clear_pattern_after_trigger: bool = True,
                             bit_depth: int = 1,
                             num_repeats: int = 0,
                             mode: str = 'pre-stored'):
        """
        Setup pattern sequence from patterns previously stored in DMD memory, either in on-the-fly pattern mode,
        or in pre-stored pattern mode. If you have uploaded patterns into the firmware and defined modes and channels,
        then, use program_dmd_seq() instead of calling this function directly. For triggering to function sensibly,
        you must be careful about what state the DMD enable and advance trigger lines are in when this function is
        called. See upload_pattern_sequence() and start_stop_sequence() for more detailed discussion.

        :param pattern_indices: DMD pattern indices
        :param exp_times:
        :param dark_times:
        :param triggered:
        :param clear_pattern_after_trigger:
        :param bit_depth:
        :param num_repeats: number of repeats. 0 repeats means repeat continuously.
        :param mode: 'pre-stored' or 'on-the-fly'
        :return:
        """
        # #########################
        # check arguments
        # #########################
        if isinstance(pattern_indices, int) or np.issubdtype(type(pattern_indices), np.integer):
            pattern_indices = [pattern_indices]
        elif isinstance(pattern_indices, np.ndarray):
            pattern_indices = pattern_indices.tolist()

        if exp_times is None:
            exp_times = self.min_time_us

        nimgs = len(pattern_indices)
        pic_indices, bit_indices = self._index_2pic_bit(pattern_indices)
        pic_indices = pic_indices.tolist()
        bit_indices = bit_indices.tolist()

        if mode == 'on-the-fly' and 0 not in bit_indices:
            raise ValueError("Known issue that if 0 is not included in the bit indices, then the patterns "
                             "displayed will not correspond with the indices supplied.")

        # if only one exp_times, apply to all patterns
        if isinstance(exp_times, int):
            exp_times = [exp_times]

        if not all(list(map(lambda t: isinstance(t, int), exp_times))):
            raise ValueError("exp_times must be a list of integers")

        if nimgs > 1 and len(exp_times) == 1:
            exp_times = exp_times * nimgs

        # if only one dark_times, apply to all patterns
        if isinstance(dark_times, int):
            dark_times = [dark_times]

        if not all(list(map(lambda t: isinstance(t, int), dark_times))):
            raise ValueError("dark_times must be a list of integers")

        if nimgs > 1 and len(dark_times) == 1:
            dark_times = dark_times * nimgs

        # #########################
        # #########################
        # need to issue stop before changing mode, otherwise DMD will sometimes lock up and not be responsive.
        self.start_stop_sequence('stop')

        # set to pattern mode
        buffer = self.set_pattern_mode(mode)
        resp = self.decode_response(buffer)
        if resp['error']:
            print(self.read_error_description())

        # stop any currently running sequences
        # note: want to stop after changing pattern mode, because otherwise may throw error
        self.start_stop_sequence('stop')

        # set image parameters for look up table_
        for ii, (et, dt) in enumerate(zip(exp_times, dark_times)):
            buffer = self._pattern_display_lut_definition(ii,
                                                          exposure_time_us=et,
                                                          dark_time_us=dt,
                                                          wait_for_trigger=triggered,
                                                          clear_pattern_after_trigger=clear_pattern_after_trigger,
                                                          bit_depth=bit_depth,
                                                          stored_image_index=pic_indices[ii],
                                                          stored_image_bit_index=bit_indices[ii])
            resp = self.decode_response(buffer)
            if resp['error']:
                print(self.read_error_description())

        # PAT_CONFIG command
        buffer = self._pattern_display_lut_configuration(nimgs, num_repeat=num_repeats)

        if buffer == []:
            print(self.read_error_description())
        else:
            resp = self.decode_response(buffer)
            if resp['error']:
                print(self.read_error_description())

        # start sequence
        self.start_stop_sequence('start')

        # some weird behavior where wants to be STOPPED before starting triggered sequence
        if triggered:
            self.start_stop_sequence('stop')

    #######################################
    # high-level commands for working with patterns and pattern sequences
    # the primary difference from the low level functions is that the high-level functions recognize
    # the concept of "channels" and "modes" describing families of DMD patterns. This information can be
    # supplied at instantiation using the "presets" argument
    #######################################
    def get_dmd_sequence(self,
                         modes: Sequence[str],
                         channels: Sequence[str],
                         nrepeats: Sequence[int] = 1,
                         noff_before: Sequence[int] = 0,
                         noff_after: Sequence[int] = 0,
                         blank: Sequence[bool] = False,
                         mode_pattern_indices: Sequence[Sequence[int]] = None):
        """
        Generate DMD patterns from a list of modes and channels

        This function requires that self.presets exists. self.presets[channel][mode] is an array of firmware pattern
        indices. These are resolved to picture and bit indices

        :param modes: modes, which refers to the keys in self.presets[channel]
        :param channels: channels, which refer to the keys in self.presets
        :param nrepeats: number of times to repeat patterns
        :param noff_before: number of "off" patterns to prepend to the start of each mode
        :param noff_after: number of "off" patternst to append to the end of each mode
        :param blank: whether to add "off" patterns after each pattern in each mode
        :param mode_pattern_indices: select subset of mode patterns to use. Each nested list contains the indices
          of the patterns in self.presets[channel][mode] to use
        :return firmware_indices:
        """
        if self.presets is None:
            raise ValueError("self.presets was None, but must be a dictionary populated with channels and modes.")

        # check channel argument
        if isinstance(channels, str):
            channels = [channels]

        if not isinstance(channels, list):
            raise ValueError(f"'channels' must be of type list, but was {type(channels)}")

        nmodes = len(channels)

        # check mode argument
        if isinstance(modes, str):
            modes = [modes]

        if not isinstance(modes, list):
            raise ValueError(f"'modes' must be of type list, but was {type(modes)}")

        if len(modes) == 1 and nmodes > 1:
            modes = modes * nmodes

        if len(modes) != nmodes:
            raise ValueError(f"len(modes)={len(modes):d} and nmodes={nmodes:d}, but these must be equal")

        # check pattern indices argument
        if mode_pattern_indices is None:
            mode_pattern_indices = []
            for c, m in zip(channels, modes):
                npatterns = len(self.presets[c][m])
                mode_pattern_indices.append(np.arange(npatterns, dtype=int))

        if isinstance(mode_pattern_indices, int):
            mode_pattern_indices = [mode_pattern_indices]

        if not isinstance(mode_pattern_indices, list):
            raise ValueError(f"'mode_pattern_indices' must be of type list, but was {type(mode_pattern_indices)}")

        if len(mode_pattern_indices) == 1 and nmodes > 1:
            mode_pattern_indices = mode_pattern_indices * nmodes

        if len(mode_pattern_indices) != nmodes:
            raise ValueError(f"len(mode_pattern_indices)={len(mode_pattern_indices):d} and "
                             f"nmodes={nmodes:d}, but these must be equal")

        # check nrepeats argument
        if isinstance(nrepeats, int):
            nrepeats = [nrepeats]

        if not isinstance(nrepeats, list):
            raise ValueError(f"'nrepeats' must be of type list, but was {type(nrepeats)}")

        if nrepeats is None:
            nrepeats = []
            for _ in zip(channels, modes):
                nrepeats.append(1)

        if len(nrepeats) == 1 and nmodes > 1:
            nrepeats = nrepeats * nmodes

        if len(nrepeats) != nmodes:
            raise ValueError(f"nrepeats={nrepeats:d} and nmodes={nmodes:d}, but these must be equal")

        # check noff_before argument
        if isinstance(noff_before, int):
            noff_before = [noff_before]

        if not isinstance(noff_before, list):
            raise ValueError(f"'noff_before' must be of type list, but was {type(noff_before)}")

        if len(noff_before) == 1 and nmodes > 1:
            noff_before = noff_before * nmodes

        if len(noff_before) != nmodes:
            raise ValueError(f"len(noff_before)={len(noff_before):d} and nmodes={nmodes:d}, but these must be equal")

        # check noff_after argument
        if isinstance(noff_after, int):
            noff_after = [noff_after]

        if not isinstance(noff_after, list):
            raise ValueError(f"'noff_after' must be of type list, but was {type(noff_after)}")

        if len(noff_after) == 1 and nmodes > 1:
            noff_after = noff_after * nmodes

        if len(noff_after) != nmodes:
            raise ValueError(f"len(noff_after)={len(noff_after):d} and nmodes={nmodes:d}, but these must be equal")

        # check blank argument
        if isinstance(blank, bool):
            blank = [blank]

        if not isinstance(blank, list):
            raise ValueError(f"'blank' must be of type list, but was {type(blank)}")

        if len(blank) == 1 and nmodes > 1:
            blank = blank * nmodes

        if len(blank) != nmodes:
            raise ValueError(f"len(blank)={len(blank):d} and nmodes={nmodes:d}, but these must be equal")

        f_inds = []
        for c, m, ind, nreps in zip(channels, modes, mode_pattern_indices, nrepeats):
            fi = np.array(np.atleast_1d(self.presets[c][m]), copy=True)
            fi = fi[ind]  # select indices
            fi = np.hstack([fi] * nreps)  # repeats
            f_inds.append(fi)

        # insert off patterns at the start or end of the sequence
        for ii in range(nmodes):
            if noff_before[ii] != 0 or noff_after[ii] != 0:
                ioff_before = self.presets[channels[ii]]["off"] * np.ones(noff_before[ii], dtype=int)
                ioff_after = self.presets[channels[ii]]["off"] * np.ones(noff_after[ii], dtype=int)
                f_inds[ii] = np.concatenate((ioff_before, f_inds[ii], ioff_after), axis=0).astype(int)

        # insert off patterns after each pattern to "blank"
        for ii in range(nmodes):
            if blank[ii]:
                npatterns = len(f_inds[ii])
                ioff = self.presets[channels[ii]]["off"]
                ioff_new = np.zeros((2 * npatterns), dtype=int)
                ioff_new[::2] = f_inds[ii]
                ioff_new[1::2] = ioff
                f_inds[ii] = ioff_new

        return np.hstack(f_inds)

    def program_dmd_seq(self,
                        modes: Sequence[str],
                        channels: Sequence[str],
                        nrepeats: Sequence[int] = 1,
                        noff_before: Sequence[int] = 0,
                        noff_after: Sequence[int] = 0,
                        blank: Sequence[bool] = False,
                        mode_pattern_indices: Sequence[Sequence[int]] = None,
                        triggered: bool = False,
                        exp_time_us: Optional[int] = None,
                        clear_pattern_after_trigger: bool = False,
                        verbose: bool = False) -> (np.ndarray, np.ndarray):
        """
        convenience function for generating DMD pattern and programming DMD

        :param modes:
        :param channels:
        :param nrepeats:
        :param noff_before:
        :param noff_after:
        :param blank:
        :param mode_pattern_indices:
        :param triggered:
        :param exp_time_us:
        :param clear_pattern_after_trigger:
        :param verbose:
        :return firmware_inds:
        """

        firmware_inds = self.get_dmd_sequence(modes,
                                              channels,
                                              nrepeats=nrepeats,
                                              noff_before=noff_before,
                                              noff_after=noff_after,
                                              blank=blank,
                                              mode_pattern_indices=mode_pattern_indices)

        self.debug = verbose
        self.start_stop_sequence('stop')
        # check DMD trigger state
        # todo: do I need this code for the triggers?
        delay1_us, mode_trig1 = self.get_trigger_in1()
        mode_trig2 = self.get_trigger_in2()

        self.set_pattern_sequence(firmware_inds,
                                  exp_time_us,
                                  triggered=triggered,
                                  clear_pattern_after_trigger=clear_pattern_after_trigger,
                                  mode='pre-stored')

        if verbose:
            print(f"{len(firmware_inds):d} firmware pattern indices: {firmware_inds}")
            print("finished programming DMD")

        return firmware_inds

    @staticmethod
    def _index_2pic_bit(firmware_indices: Sequence[int]) -> (np.ndarray, np.ndarray):
        """
        convert from single firmware pattern index to picture and bit indices

        :param firmware_indices:
        :return pic_inds, bit_inds:
        """
        pic_inds = np.asarray(firmware_indices) // 24
        bit_inds = firmware_indices - 24 * np.asarray(pic_inds)

        return pic_inds, bit_inds

    @staticmethod
    def _pic_bit2index(pic_inds: Sequence[int],
                       bit_inds: Sequence[int]) -> np.ndarray:
        """
        Convert from picture and bit indices to single firmware pattern index

        :param pic_inds:
        :param bit_inds:
        :return firmware_inds:
        """
        firmware_inds = np.asarray(pic_inds) * 24 + np.asarray(bit_inds)
        return firmware_inds


class dlp6500(dlpc900_dmd):
    width = 1920  # pixels
    height = 1080  # pixels
    pitch = 7.56  # um
    dual_controller = False

    def __init__(self, *args, **kwargs):
        super(dlp6500, self).__init__(*args, **kwargs)


# Include this alias for dlp6500 to avoid external code changes
dlp6500win = dlp6500


class dlp9000(dlpc900_dmd):
    width = 2048  # pixels
    height = 1200  # pixels
    pitch = 5.4  # um
    dual_controller = True

    def __init__(self, *args, **kwargs):
        super(dlp9000, self).__init__(*args, **kwargs)


if __name__ == "__main__":
    # #######################
    # load config fiile
    # #######################

    fname = "dmd_config.zarr"
    try:
        pattern_data, presets, _, _, _ = load_config_file(fname)
    except FileNotFoundError:
        raise FileNotFoundError(f"configuration file `{fname:s}` was not found. For the command line parser to work,"
                                f"create this file using save_config_file(), and place it in the same"
                                f" directory as dlp6500.py")

    # #######################
    # load DLP6500 or DLP9000
    # #######################
    _dmd_helper = dlpc900_dmd()
    dmd_model = _dmd_helper.get_firmware_type()["dmd type"]
    del _dmd_helper

    if dmd_model == "DLP6500":
        cls = dlp6500
    elif dmd_model == "DLP9000":
        cls = dlp9000
    else:
        raise NotImplementedError(f"DMD model {dmd_model:s} is not supported or has not been tested")

    dmd = cls(firmware_pattern_info=pattern_data, presets=presets)

    # #######################
    # define arguments
    # #######################

    parser = ArgumentParser(description="Set DMD pattern sequence from the command line.")

    # allowed channels
    all_channels = list(presets.keys())
    parser.add_argument("channels", type=str, nargs="+", choices=all_channels,
                        help="supply the channels to be used in this acquisition as strings separated by spaces")

    # allowed modes
    modes = list(set([m for c in all_channels for m in list(presets[c].keys())]))
    modes_help = "supply the modes to be used with each channel as strings separated by spaces." \
                 "each channel supports its own list of modes.\n"
    for c in all_channels:
        modes_with_parenthesis = ["'%s'" % m for m in list(presets[c].keys())]
        modes_help += ("channel '%s' supports: " % c) + ", ".join(modes_with_parenthesis) + ".\n"

    parser.add_argument("-m", "--modes", type=str, nargs=1, choices=modes, default="default",
                        help=modes_help)

    # pattern indices
    pattern_indices_help = "Among the patterns specified in the subset specified by `channels` and `modes`," \
                           " only run these indices. For a given channel and mode, allowed indices range from 0 " \
                           "to npatterns - 1. This options is most commonly used when only a single channel and " \
                           "mode are provided.\n"
    for c in list(presets.keys()):
        for m in list(presets[c].keys()):
            pattern_indices_help += f"channel '{c:s}' and " \
                                    f"mode '{m:s}' " \
                                    f"npatterns = {len(presets[c][m]['picture_indices']):d}.\n"

    parser.add_argument("-i", "--pattern_indices", type=int, help=pattern_indices_help)

    parser.add_argument("-r", "--nrepeats", type=int, default=1,
                        help="number of times to repeat the patterns specificed by `channels`, "
                             "`modes`, and `pattern_indices`")

    # other
    parser.add_argument("-t", "--triggered", action="store_true",
                        help="set DMD to wait for trigger before switching pattern")
    parser.add_argument("-d", "--noff_before", type=int, default=0,
                        help="set number of off frames to be added before each channel/mode combination")
    parser.add_argument("-d", "--noff_after", type=int, default=0,
                        help="set number of off frames to be added after each channel/mode combination")
    parser.add_argument("-b", "--blank", action="store_true",
                        help="set whether or not to insert off patterns after each pattern in "
                             "each channel/mode combination to blank laser")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="print more verbose DMD programming information")
    parser.add_argument("--illumination_time", type=int, default=105,
                        help="illumination time in microseconds. Ignored if triggered is true")
    args = parser.parse_args()

    if args.verbose:
        print(args)

    dmd.program_dmd_seq(args.modes,
                        args.channels,
                        nrepeats=args.nrepeats,
                        noff_before=args.noff_before,
                        noff_after=args.noff_after,
                        blank=args.blank,
                        mode_pattern_indices=args.pattern_indices,
                        triggered=args.triggered,
                        exp_time_us=args.illumination_time,
                        verbose=args.verbose
                        )