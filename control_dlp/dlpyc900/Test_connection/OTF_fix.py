import dlpyc900.dlp as dlpyc900
import usb.core
import pretty_errors
import time
from PIL import Image
import os, sys
import numpy as np
import dlpyc900.erle as erle


# DLP 장치 찾기
dev = usb.core.find(idVendor=0x0451, idProduct=0xc900)
if dev is None:
    raise ValueError("DMD device not found")

interface = 0  # 보통 0번 인터페이스 사용

# 리눅스에서 커널 드라이버가 인터페이스를 점유하고 있다면 분리
if dev.is_kernel_driver_active(interface):
    print("커널 드라이버가 활성화되어 있어 분리 중...")
    dev.detach_kernel_driver(interface)


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
# image_folder = "/home/jhchang/dlpyc900/LCR500YX_Images/"
image_folder = "/home/jhchang/dlpyc900/Test_image/"
# filename = "0666.bmp"
filename = "kist_image.bmp"
image_path = os.path.join(image_folder, filename)
img = Image.open(image_path).convert("1")
img_array = np.array(img, dtype= np.uint8)


mid_x = img_array.shape[1] // 2
left_half = img_array[:, :mid_x]
# left_half = np.flipud(left_half)

right_half = img_array[:, mid_x:]
# right_half = np.flipud(right_half)

def rgb24(img_bin: np.ndarray) -> np.ndarray:
    """
    1비트 2D numpy array (0,1) → 24비트 3채널 RGB numpy array (uint8)
    """
    assert img_bin.dtype == np.uint8, "Input must be uint8."
    assert img_bin.ndim == 2, "Input must be 2D array."

    height, width = img_bin.shape
    img_rgb = np.zeros((height, width, 3), dtype=np.uint8)
    img_rgb[img_bin == 1] = [255, 255, 255]  # 흰색

    return img_rgb

def rle_encode_dlp(img: np.ndarray) -> bytes:
    """
    TI 방식 Special RLE 압축 (repeat/copy from previous line/raw) 구현
    - img : (height, width, 3) uint8 numpy array, RGB24 포맷
    - return : 압축된 바이트열
    """
    height, width, _ = img.shape
    output = bytearray()
    prev_line = None

    for y in range(height):
        line = img[y]
        x = 0
        raw = 0
        while x < width:
            # 같은 픽셀 반복 찾기
            repeat = 1
            while x + repeat < width and np.array_equal(line[x], line[x + repeat]):
                repeat += 1

            # 이전 라인과 같은 픽셀 찾기
            copy = 0
            if prev_line is not None:
                while x + copy < width and np.array_equal(line[x + copy], prev_line[x + copy]):
                    copy += 1

            if copy > 0 and copy >= repeat:
                # 이전 라인 복사
                if raw > 0:
                    # raw pixel 먼저 기록
                    output.append(0x00)
                    output.append(raw)
                    for i in range(raw):
                        output.extend(bytes(line[x - raw + i][::-1])) # BGR 순서로 저장
                # 복사 명령
                output.append(0x00)
                output.append(0x01)
                if copy < 128:
                    output.append(copy)
                    output.append((copy & 0x7F) | 0x80)
                    output.append(copy >> 7)
                x += copy
                raw = 0
            elif repeat > 1:
                # 반복 픽셀 기록
                if raw > 0:
                    output.append(0x00)
                    output.append(raw)
                    for i in range(raw):
                        output.extend(bytes(line[x - raw + i][::-1]))

                if repeat < 128:
                    output.append(repeat)
                else:
                    output.append((repeat & 0x7F) | 0x80)
                    output.append(repeat >> 7)
                output.extend(bytes(line[x][::-1]))  # 반복하는 픽셀 하나
                x += repeat
                raw = 0
            else:
                # 그냥 raw 모으기
                x += 1
                raw += 1

        # 한 줄 끝나고 raw 남았으면 기록
        if raw > 0:
            output.append(0x00)
            output.append(raw)
            for i in range(raw):
                output.extend(bytes(line[x - raw + i][::-1]))

        prev_line = line

    # end of image
    output.append(0x00)
    output.append(0x01)
    output.append(0x00)

    return bytes(output)

compression = True

if compression == True:
    left_half = rgb24(left_half)
    primary_data = rle_encode_dlp(left_half)

    right_half = rgb24(right_half)
    secondary_data = rle_encode_dlp(right_half)

    left_size, primary_data = len(list(primary_data)), list(primary_data)
    right_size, secondary_data = len(list(secondary_data)), list(secondary_data)
########################################################################################
else:
    left_half = rgb24(left_half)
    left_half = rgb24(right_half)
    left_size, primary_data = len(list(left_half.tobytes())), list(left_half.tobytes())
    right_size, secondary_data = len(list(right_half.tobytes())), list(right_half.tobytes())

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

