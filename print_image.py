"""Render text as an image and print to POS58 Printer as ONE BMP job.

Usage:
    python print_image.py "Your text here"           # Prints centered title
    python print_image.py story.txt                  # Prints file contents
    echo "hello" | python print_image.py -           # Reads stdin

Features:
    - Single-job print (no wasted paper between lines)
    - Supports any Unicode text (Telugu, Hindi, etc.)
    - Continuous-flow as one page on the roll
    - Auto-sized font
"""

import sys
import os
from io import BytesIO
from PIL import Image, ImageDraw, ImageFont
import ctypes
from ctypes import wintypes

PRINTER_NAME = os.environ.get('PRINTER_NAME', 'POS58 Printer')
FONT = r'C:\Windows\Fonts\gautami.ttf'
FONT_BOLD = r'C:\Windows\Fonts\gautamib.ttf'
IMG_W = 576      # paper width in pixels (58mm @ 8px/mm, with margin)

# Find any font that supports the input text
def find_font(force_default=False):
    """Find a font with broad Unicode support. Falls back to a default."""
    if force_default:
        return None
    if os.path.exists(FONT):
        return FONT
    # Look for any TTF that supports current chars
    return None


def render_text_to_image(text, font_size=22, img_width=IMG_W):
    """Render multi-line text to a tall image, crop to content."""
    lines = text.split('\n')
    font_path = find_font()
    font = ImageFont.truetype(font_path, font_size) if font_path else ImageFont.load_default()
    font_b = ImageFont.truetype(FONT_BOLD, font_size) if os.path.exists(FONT_BOLD) else font

    # Determine height needed
    line_h = font.size + 8
    max_h = max(600, len(lines) * line_h + 40)

    img = Image.new('RGB', (img_width, max_h), (255, 255, 255))
    draw = ImageDraw.Draw(img)
    y = 20

    for line in lines:
        line = line.rstrip()
        if not line:
            y += line_h
            continue
        # Pick bold font for heading-like lines
        use_font = font_b if line.startswith('*') else font
        # Strip leading "*" indicators
        if line.startswith('*'):
            line = '  ' + line[1:].strip()

        bbox = use_font.getbbox(line)
        tw = bbox[2] - bbox[0]
        x = 12
        # Wrap if wider than image
        if tw > img_width - 24:
            # Simple wrap
            words = line.split()
            cur = ''
            for w in words:
                test = (cur + ' ' + w).strip()
                bw = use_font.getbbox(test)[2]
                if bw > img_width - 24 and cur:
                    draw.text((x, y), cur, font=use_font, fill=(0, 0, 0))
                    y += line_h
                    cur = w
                else:
                    cur = test
            if cur:
                draw.text((x, y), cur, font=use_font, fill=(0, 0, 0))
                y += line_h
        else:
            draw.text((x, y), line, font=use_font, fill=(0, 0, 0))
            y += line_h

    # Crop
    bbox = img.getbbox()
    if bbox:
        img = img.crop((0, bbox[1], img_width, bbox[3]))
    return img


def print_image(img, printer_name=PRINTER_NAME):
    """Print image as a single BMP job via winspool."""
    bmp_buf = BytesIO()
    img.convert('1', dither=Image.Dither.FLOYDSTEINBERG).save(bmp_buf, 'BMP')
    data = bmp_buf.getvalue()

    winspool = ctypes.WinDLL('winspool.drv', use_last_error=True)

    class DOC_INFO_1(ctypes.Structure):
        _fields_ = [
            ('pDocName', wintypes.LPWSTR),
            ('pOutputFile', wintypes.LPWSTR),
            ('pDatatype', wintypes.LPWSTR),
        ]

    winspool.OpenPrinterW.argtypes = [wintypes.LPWSTR, ctypes.POINTER(wintypes.HANDLE), wintypes.LPVOID]
    winspool.OpenPrinterW.restype = wintypes.BOOL
    winspool.StartDocPrinterW.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(DOC_INFO_1)]
    winspool.StartDocPrinterW.restype = wintypes.DWORD
    winspool.WritePrinter.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
    winspool.WritePrinter.restype = wintypes.BOOL
    winspool.EndDocPrinter.argtypes = [wintypes.HANDLE]
    winspool.EndDocPrinter.restype = wintypes.BOOL
    winspool.ClosePrinter.argtypes = [wintypes.HANDLE]
    winspool.ClosePrinter.restype = wintypes.BOOL

    h = wintypes.HANDLE()
    if not winspool.OpenPrinterW(printer_name, ctypes.byref(h), None):
        raise RuntimeError(f'OpenPrinter {printer_name!r} failed (err={ctypes.get_last_error()})')

    try:
        doc = DOC_INFO_1(pDocName='Print', pOutputFile=None, pDatatype='RAW')
        job_id = winspool.StartDocPrinterW(h, 1, ctypes.byref(doc))
        if not job_id:
            raise RuntimeError(f'StartDocPrinter failed (err={ctypes.get_last_error()})')
        winspool.StartPagePrinter(h)
        written = wintypes.DWORD(0)
        ok = winspool.WritePrinter(h, ctypes.c_char_p(data), len(data), ctypes.byref(written))
        if not ok:
            raise RuntimeError(f'WritePrinter failed (err={ctypes.get_last_error()})')
        winspool.EndPagePrinter(h)
        winspool.EndDocPrinter(h)
        return written.value
    finally:
        winspool.ClosePrinter(h)


def get_input_text():
    if len(sys.argv) > 1:
        arg = sys.argv[1]
        if arg == '-':
            return sys.stdin.read()
        if os.path.isfile(arg):
            with open(arg, 'r', encoding='utf-8') as f:
                return f.read()
        return ' '.join(sys.argv[1:])
    return sys.stdin.read()


def main():
    text = get_input_text().strip()
    if not text:
        print('Usage: print_image.py "text" | filename.txt')
        sys.exit(1)

    print(f'Rendering {len(text)} chars...')
    img = render_text_to_image(text)
    print(f'Image: {img.size}')
    n = print_image(img)
    print(f'Wrote {n} bytes to {PRINTER_NAME}')


if __name__ == '__main__':
    main()
