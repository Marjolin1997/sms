"""Numërimi i segmenteve: GSM-7 (160/153) ose UCS-2 (70/67)."""

GSM7_BASIC = (
    "@£$¥èéùìòÇ\nØø\rÅåΔ_ΦΓΛΩΠΨΣΘΞÆæßÉ !\"#¤%&'()*+,-./0123456789:;<=>?"
    "¡ABCDEFGHIJKLMNOPQRSTUVWXYZÄÖÑÜ§¿abcdefghijklmnopqrstuvwxyzäöñüà"
)
GSM7_EXT = "^{}\\[~]|€"  # numërohen si 2 karaktere


def encoding_and_length(text: str) -> tuple[str, int]:
    length = 0
    for ch in text:
        if ch in GSM7_BASIC:
            length += 1
        elif ch in GSM7_EXT:
            length += 2
        else:
            return "ucs2", sum(2 if ord(c) > 0xFFFF else 1 for c in text)
    return "gsm7", length


def count_segments(text: str) -> tuple[str, int]:
    if not text:
        raise ValueError("empty message")
    enc, n = encoding_and_length(text)
    single, multi = (160, 153) if enc == "gsm7" else (70, 67)
    segments = 1 if n <= single else -(-n // multi)
    return enc, segments
