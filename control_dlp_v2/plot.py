import pywinusb.hid as hid

def find_dlpc900_device(vendor_id=0x0451, product_id=0xc900):
    # DLPC900은 TI의 제품 ID와 벤더 ID를 사용
    all_devices = hid.HidDeviceFilter(vendor_id=vendor_id, product_id=product_id).get_devices()

    for device in all_devices:
        if device.product_name == "DLPC900":
            return device

    raise RuntimeError("DLPC900 DMD not found.")

def connect_and_send(device):
    # 연결
    device.open()

    # 응답 핸들러 설정 (비동기 수신용)
    def read_handler(data):
        print("Received data:", data)

    device.set_raw_data_handler(read_handler)

    # 출력 레포트 찾기
    output_reports = device.find_output_reports()
    if not output_reports:
        raise RuntimeError("No output reports found")

    # 예시: 64바이트의 zero-padding USB 패킷 전송
    packet = [0x00] + [0x00] * 64  # report_id + 64바이트 패킷
    output_reports[0].send(packet)

    input("Press Enter to close device...")

    device.close()


device = find_dlpc900_device()
connect_and_send(device)
