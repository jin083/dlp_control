import dlpyc900.dlp as dlpyc900
import usb.core
import pretty_errors
import time
from PIL import Image
import os, sys

# DLP 장치 찾기
dev = usb.core.find(idVendor=0x0451, idProduct=0xc900)
if dev is None:
    raise ValueError("DMD device not found")

interface = 0  # 보통 0번 인터페이스 사용

# 리눅스에서 커널 드라이버가 인터페이스를 점유하고 있다면 분리
if dev.is_kernel_driver_active(interface):
    print("커널 드라이버가 활성화되어 있어 분리 중...")
    dev.detach_kernel_driver(interface)
reset = True


# define class dlp
dlp=dlpyc900.dmd()

# print status of DLP product(Optional)
dlp.stop_pattern()

# set OTF mode
dlp.set_display_mode('otf')
print(dlp.get_display_mode())
# Add a pattern to the Look Up Table & Start displaying patterns from the Look Up Table (LUT)

dlp.setup_pattern_LUT_definition(pattern_index = 0, exposuretime = 200000, color = 7, bitdepth = 1, image_pattern_index = 0, bit_position = 0) # call this function as many times as the number of patterns you want to upload
# dlp.setup_pattern_LUT_definition(pattern_index = 1, exposuretime = 15000, color = 7, bitdepth = 1, image_pattern_index = 0, bit_position = 1) # call this function as many times as the number of patterns you want to upload
dlp.start_pattern_from_LUT(nr_of_LUT_entries=1, nr_of_patterns_to_display= 0)

# option - trun on praimary & secondary controler
turn_on = 1

if turn_on == 1: 
    controller_status = dlp.send_command('w', 10, 0x0031, [3])

# processing bmp image
image_folder = "/home/jhchang/dlpyc900/LCR500YX_Images/"
filename = "0010.bmp"
image_path = os.path.join(image_folder, filename)
img = Image.open(image_path)

left_data, right_data = dlpyc900.load_bmp_halves_as_1bit_array(img, com = True)


compression = True
comp = 0
left_data = left_data.flatten()
right_data = right_data.flatten()

if compression is True:
    left_data = dlpyc900.run_length_encode(left_data)
    right_data = dlpyc900.run_length_encode(right_data)
    comp = 1

# Load bmp file
dlp.initialize_pattern_bmp_load_fix(0, len(left_data),  len(right_data))
dlp.pattern_bmp_load_fix(left_data, right_data)

# Start running the patterns  
dlp.start_pattern()

# print(dlp.get_display_mode())
# dlp.stop_pattern()

# time.sleep(2)

# dlp.set_display_mode('pattern')
# print(dlp.get_display_mode())

# # dlp.standby()

# # time.sleep(5)

# dlp.reset()
# print(dlp.get_display_mode())

# dlp.start_pattern()

