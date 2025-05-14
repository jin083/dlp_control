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
time.sleep(2)
# set OTF mode
dlp.set_display_mode('pattern')


print(dlp.get_display_mode())
# dlp.standby()
# Add a pattern to the Look Up Table & Start displaying patterns from the Look Up Table (LUT)
dlp.setup_pattern_LUT_definition(pattern_index = 0, exposuretime = 200000, color = 7, bitdepth = 1, image_pattern_index = 3) # call this function as many times as the number of patterns you want to upload
dlp.setup_pattern_LUT_definition(pattern_index = 1, exposuretime = 200000, color = 7, bitdepth = 1, image_pattern_index = 4, bit_position = 1) # call this function as many times as the number of patterns you want to upload
dlp.start_pattern_from_LUT(nr_of_LUT_entries=2, nr_of_patterns_to_display= 0)

# option - trun on praimary & secondary controler


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

