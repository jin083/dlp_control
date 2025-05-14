import dlpyc900.dlp as dlpyc900
import usb.core
import usb.util
from termcolor import colored
import pretty_errors
import sys

def highlight_error(message: str) -> str:
    return colored("[ERROR] " + message, "red", attrs=["bold"])

def print_dmd_status():
    try:
        # 디바이스 수동으로 탐색 및 드라이버 분리
        dev = usb.core.find(idVendor=0x0451, idProduct=0xc900)
        if dev is None:
            print(highlight_error("DMD device not found (VID:0451, PID:c900)"))
            sys.exit(1)

        # 인터페이스 번호 (보통 0)
        interface = 0
        if dev.is_kernel_driver_active(interface):
            print("[INFO] Kernel driver active — detaching...")
            dev.detach_kernel_driver(interface)
        else:
            print("[INFO] Kernel driver already detached or not active.")

        # DMD 객체로 실제 사용
        with dlpyc900.dmd() as d:
            print("====" * 4, "DMD STATUS", "====" * 4)

            # Display mode
            display_mode = d.get_display_mode()
            print(f"Display Mode       : {display_mode}")

            # Hardware 정보
            hardware, firmware = d.get_hardware()
            print(f"Hardware           : {hardware}")
            print(f"Firmware           : {firmware}")

            # Main status
            status_fields = [
                "Micromirrors parked",
                "Sequencer running",
                "Video frozen",
                "External source locked",
                "Port 1 sync valid",
                "Port 2 sync valid"
            ]
            main_status = d.get_main_status()
            print("Main Status        :")
            for i, val in enumerate(main_status):
                print(f"  {status_fields[i]:25}: {'Yes' if val else 'No'}")

            # Power mode
            power_mode = d.get_current_powermode()
            print(f"Power Mode         : {power_mode}")

            # Hardware status with error highlighting
            hw_status, hw_errors = d.get_hardware_status()
            print("Hardware Status    :")
            for line in hw_status.strip().split("\n"):
                if "Error" in line or "Incompatible" in line:
                    print("  " + highlight_error(line))
                else:
                    print("  " + line)
            print(f"Hardware Errors    : {hw_errors}")
            print("====================" * 2 + "=====")

    except usb.core.USBError as e:
        print(highlight_error(f"USB communication failed: {e}"))
        sys.exit(1)
    except Exception as e:
        print(highlight_error(f"Unexpected error: {e}"))
        sys.exit(1)

# if __name__ == "__main__":
#     print_dmd_status()


dlp=dlpyc900.dmd()
dlp.start_pattern()
