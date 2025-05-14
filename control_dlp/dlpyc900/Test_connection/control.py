import sys, os
sys.path.insert(0, os.path.abspath('..'))  # 상위 폴더 경로를 추가

import dlpyc900.dlp as dlpyc900
import usb.core
import time, pretty_errors 

# DLP 장치 찾기
dev = usb.core.find(idVendor=0x0451, idProduct=0xc900)
if dev is None:
    raise ValueError("DMD device not found")

interface = 0  # 보통 0번 인터페이스 사용

# 리눅스에서 커널 드라이버가 인터페이스를 점유하고 있다면 분리
if dev.is_kernel_driver_active(interface):
    print("커널 드라이버가 활성화되어 있어 분리 중...")
    dev.detach_kernel_driver(interface)


dlp=dlpyc900.dmd()

controller_status = dlp.send_command('r', 10, 0x0031, [])
print(controller_status)
# dlp.set_display_mode('pattern')
# print(dlp.get_display_mode())
# for i in range(200): 
#     dlp.setup_pattern_LUT_definition(exposuretime=1000000, pattern_index = i, bit_position = 1)
# # dlp.setup_pattern_LUT_definition(pattern_index = 2)
# dlp.start_pattern_from_LUT(nr_of_LUT_entries =200)
# dlp.start_pattern()


# dlp.send_command('w', 0x0033, )

# for i in range(300):
#     image = dlp.send_command('r',10 , 0x1A39 , [i])
#     print(image)

