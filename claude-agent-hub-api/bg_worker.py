"""Background removal in its own process: image bytes on stdin, PNG with transparency on stdout.

Runs apart from the web server so that if rembg runs out of memory or crashes, only this process dies:
the server stays up and the agent gets an error and carries on with the rest of its task.
"""
import io
import sys

MAX_SIDE = 2000  # larger photos are scaled down first: same result, far less memory


def main():
    data = sys.stdin.buffer.read()
    from PIL import Image
    from rembg import new_session, remove

    img = Image.open(io.BytesIO(data))
    img.load()
    if max(img.size) > MAX_SIDE:
        img.thumbnail((MAX_SIDE, MAX_SIDE))
    if img.mode not in ('RGB', 'RGBA'):
        img = img.convert('RGBA')
    out = remove(img, session=new_session('isnet-general-use'))
    buf = io.BytesIO()
    out.save(buf, format='PNG')
    sys.stdout.buffer.write(buf.getvalue())


if __name__ == '__main__':
    main()
