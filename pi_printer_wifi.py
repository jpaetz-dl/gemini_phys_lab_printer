import time
import requests
import base64
import io
from PIL import Image
from escpos.printer import Usb, Serial  # Import Usb or Serial depending on connection

# ==========================================
# CONFIGURATION
# ==========================================
# 1. Enter your Laptop's Wi-Fi IP Address running the Express server
LAPTOP_IP = "10.18.44.99"  # <--- Change this to your laptop's local IP address
SERVER_URL = f"http://{LAPTOP_IP}:5005/api/print/latest"

# 2. Select your Connection Mode (Set to True for USB, False for Bluetooth Serial)
USE_USB = True

# USB IDs (From running lsusb on the Pi)
VENDOR_ID = 0x0483    # Replace with their actual Vendor ID hex
PRODUCT_ID = 0x5743   # Replace with their actual Product ID hex

# ==========================================
# PRINT RUNNER
# ==========================================
def monitor_and_print():
    print("==================================================")
    print("📠 Raspberry Pi Wireless Thermal Printer Daemon Active!")
    print(f"Polling print queue at: {SERVER_URL}")
    print("==================================================")
    
    last_printed_timestamp = 0

    while True:
        try:
            # Poll the laptop's Express server over Wi-Fi
            response = requests.get(SERVER_URL, timeout=3)
            if response.status_code == 200:
                data = response.json()
                job_timestamp = data.get("timestamp", 0)
                base64_image = data.get("image", "")

                # If there is a new print job in the queue
                if job_timestamp > last_printed_timestamp and base64_image:
                    print(f"\n[+] New receipt job detected! Timestamp: {job_timestamp}")
                    
                    # Clean the base64 prefix
                    clean_base64 = base64_image.replace("data:image/jpeg;base64,", "")
                    clean_base64 = clean_base64.replace("data:image/png;base64,", "")
                    
                    # Decode the image bytes and load into memory
                    image_bytes = base64.b64decode(clean_base64)
                    image_obj = Image.open(io.BytesIO(image_bytes))
                    print(image_obj.size)
                    print("Connecting to ETprin M860 Thermal Printer...")
                    if USE_USB:
                        p = Usb(VENDOR_ID, PRODUCT_ID)
                    else:
                        p = Serial(BT_PORT, baudrate=9600)

                    try:
                        print("Printing dithered receipt raster...")
                        # Automatically scales and prints the image onto the 58mm paper roll perfectly
                        p.image(image_obj, impl="bitImageColumn")

                        # Feed and cut paper
                        print("Feeding and slicing paper...")
                        p.text("\n\n\n\n")
                        p.cut()

                        print("[-] Print job completed successfully!")
                        last_printed_timestamp = job_timestamp
                    finally:
                        # Release the USB/serial handle so the next job can reconnect
                        p.close()
                    
        except requests.exceptions.RequestException:
            # Silently tolerate laptop/network offline state and retry
            pass
        except Exception as e:
            print(f"[-] Printer error: {e}")
            
        time.sleep(1) # Poll once per second

if __name__ == "__main__":
    monitor_and_print()
