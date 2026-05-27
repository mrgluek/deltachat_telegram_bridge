import io
import logging
import os
from typing import Optional

try:
    import qrcode as qrcode_lib
except ImportError:
    qrcode_lib = None

logger = logging.getLogger("tg_dc_bridge")


class QRCodeGenerator:
    @staticmethod
    def generate_qr_image(data: str) -> Optional[io.BytesIO]:
        if not qrcode_lib:
            return None
        try:
            qr = qrcode_lib.QRCode(version=1, box_size=10, border=4)
            qr.add_data(data)
            qr.make(fit=True)
            img = qr.make_image(fill_color="black", back_color="white")
            bio = io.BytesIO()
            bio.name = 'qr.png'
            img.save(bio, 'PNG')
            bio.seek(0)
            return bio
        except Exception as e:
            logger.error(f"Failed to generate QR image: {e}")
            return None

    @staticmethod
    def normalize_qr_data(data: str) -> str:
        if data.startswith("OPEN-CHAT:"):
            return "https://i.delta.chat/#" + data[10:]
        elif data.startswith("OPEN:"):
            return "https://i.delta.chat/#" + data[5:]
        return data

    @staticmethod
    def is_qrcode_available() -> bool:
        return qrcode_lib is not None


qr_generator = QRCodeGenerator()
