import dlpyc900.dlp as dlpyc900
import usb.core
import pretty_errors
import time
from PIL import Image
import os, sys
import numpy as np
import dlpyc900.erle as erle


import platform




# DLP 장치 찾기
dev = usb.core.find(idVendor=0x0451, idProduct=0xc900)
if dev is None:
    raise ValueError("DMD device not found")

interface = 0  # 보통 0번 인터페이스 사용

# 리눅스에서 커널 드라이버가 인터페이스를 점유하고 있다면 분리

if platform.system() != "Windows":
    if dev.is_kernel_driver_active(interface):
        print("커널 드라이버가 활성화되어 있어 분리 중...")
        dev.detach_kernel_driver(interface)


# if dev.is_kernel_driver_active(interface):
#     print("커널 드라이버가 활성화되어 있어 분리 중...")
#     dev.detach_kernel_driver(interface)


# define class dlp
dlp=dlpyc900.dmd()

# print status of DLP product(Optional)
dlp.stop_pattern() 

# set OTF mode

dlp.set_display_mode('otf')
print(dlp.get_display_mode())

dlp.stop_pattern() 
# dlp.send_command('w', 99, 0x0031, [0])


# option - trun on praimary & secondary controler
# turn_on = 1

# if turn_on == 1: 
#     dlp.send_command('w', 10, 0x0031, [0]) 


# processing bmp image
########################################################################################
# image_folder = "LCR500YX_Images/"
# image_folder = "/home/jhchang/dlpyc900/Test_image/"
image_folder = "Test_image"
# filename = "0000.bmp"
filename = "kist_image.bmp"
# filename = "2048x1200_output.bmp"
image_path = os.path.join(image_folder, filename)
img = Image.open(image_path).convert("1")
img_array = np.array(img, dtype= np.uint8)


mid_x = img_array.shape[1] // 2
left_half = img_array[:, :mid_x]
left_half = np.flipud(left_half)

right_half = img_array[:, mid_x:]
right_half = np.flipud(right_half)


########################################################################################
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


compression = True

if compression == True:
    left_img = left_half * 255
    left_rgb_img = np.stack([left_img, left_img, left_img], axis=0)

    primary_data = encode_erle(left_rgb_img)

    right_img = right_half * 255
    right_rgb_img = np.stack([right_img, right_img, right_img], axis=0)

    secondary_data = encode_erle(right_rgb_img)

    left_size, primary_data = len(primary_data), primary_data
    right_size, secondary_data = len(secondary_data), secondary_data
########################################################################################
else:
    pass
    # left_half = rgb24(left_half)
    # left_half = rgb24(right_half)
    # left_size, primary_data = len(left_half.toytes())), left_half.toytes())
    # right_size, secondary_data = len(right_half.tbytes())), right_half.tbytes())

########################################################################################
primary_header = [0x53, 0x70, 0x6C, 0x64]
secondary_header = [0x53, 0x70, 0x6C, 0x64]

primary_header += dlpyc900.bits_to_bytes(dlpyc900.number_to_bits(int(2048/2) ,bitlen = 16)) #width
secondary_header += dlpyc900.bits_to_bytes(dlpyc900.number_to_bits(int(2048/2) ,bitlen = 16)) #width

primary_header += dlpyc900.bits_to_bytes(dlpyc900.number_to_bits(1200,bitlen = 16)) #height
secondary_header += dlpyc900.bits_to_bytes(dlpyc900.number_to_bits(1200,bitlen = 16)) #height

### image size(헤더 포함인지 아닌지 확인)
primary_header += dlpyc900.bits_to_bytes(dlpyc900.number_to_bits(left_size ,bitlen = 32))
secondary_header += dlpyc900.bits_to_bytes(dlpyc900.number_to_bits(right_size ,bitlen = 32))

background_color = [0x00, 0x00, 0x00, 0x00] 

primary_header += [0xFF] * 8 + background_color
secondary_header += [0xFF] * 8 + background_color

if compression == True:
    compress = 2
else: 
    compress = 0
    
primary_header += [0x00] + [compress] + [0x01] + [0x00] *21
secondary_header += [0x00] + [compress] + [0x01] + [0x00] *21

primary_header.extend(primary_data)
secondary_header.extend(secondary_data)
########################################################################################

primary_data, secondary_data = primary_header, secondary_header
left_size, right_size = len(primary_header), len(secondary_header)
# primary_data, left_size = erle.encode([left_half])
# secondary_data, right_size = erle.encode([right_half])

# Load bmp file
# dlp.initialize_pattern_bmp_load_fix(0, left_size, right_size)
# dlp.pattern_bmp_load_fix(primary_data, secondary_data)

# Add a pattern to the Look Up Table & Start displaying patterns from the Look Up Table (LUT)
dlp.setup_pattern_LUT_definition(pattern_index = 0 ,exposuretime = 200000, color = 7, bitdepth = 1, image_pattern_index = 0, bit_position = 0) # call this function as many times as the number of patterns you want to upload
# dlp.setup_pattern_LUT_definition(pattern_index = 0 ,exposuretime = 200000, color = 7, bitdepth = 1, image_pattern_index = 0, bit_position = 0) # call this function as many times as the number of patterns you want to upload
# dlp.setup_pattern_LUT_definition(pattern_index = 1, exposuretime = 15000, color = 2, bitdepth = 1) # call this function as many times as the number of patterns you want to upload
dlp.start_pattern_from_LUT(nr_of_LUT_entries=1, nr_of_patterns_to_display= 0)



dlp.initialize_pattern_bmp_load_v2(0, left_size)
dlp.pattern_bmp_load_v2(primary_data)

# dlp.setup_pattern_LUT_definition(pattern_index = 1, exposuretime = 200000, color = 7, bitdepth = 1, image_pattern_index = 0, bit_position = 0) # call this function as many times as the number of patterns you want to upload
# dlp.start_pattern_from_LUT(nr_of_LUT_entries=1, nr_of_patterns_to_display= 0)
# 

dlp.initialize_pattern_bmp_load_v2(0, right_size, False)
dlp.pattern_bmp_load_v2(secondary_data, False)


# dlp.initialize_pattern_bmp_load(0, primary_data, secondary_data)
# dlp.pattern_bmp_load(primary_data, secondary_data)

# Start running the patterns  
dlp.start_pattern()

