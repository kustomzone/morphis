from bisect import bisect_left

import mbase32

accept_chars = b" !\"#$%&`()*+,-./0123456789:;<=>?@ABCDEFGHIJKLMNOPQRSTUVWXYZ[\\]^_'abcdefghijklmnopqrstuvwxyz{|}~"

accept_chars = sorted(accept_chars)

width = 16

def hex_dump(data, offset = 0, length = None):
    output = bytearray()
    col1 = bytearray()
    col2 = bytearray()

    if length == None:
        length = len(data)

    line = 0
    i = offset
    while i < length:
        j = 0
        while j < width and i < length:
            val = data[i]
            col1 += format(val, "02x").encode()

            si = bisect_left(accept_chars, data[i])
            if si != len(accept_chars) and accept_chars[si] == data[i]:
                col2.append(data[i])
            else:
                col2 += b'.'

            if j % 2 == 1:
                col1 += b' '

            j += 1
            i += 1

        output += format(line * width, "#06x").encode()
        output += b"   "
        line += 1
        while len(col1) < (width*5/2):
            col1 += b' '

        output += col1
        output += b' '
        output += col2
        output += b'\n'
        col1.clear()
        col2.clear()

    return output.decode()

bc_masks = [0x2, 0xC, 0xF0]
bc_shifts = [1, 2, 4]

def log_base2_8bit(val):
    r = 0

    for i in range(2, -1, -1):
        if val & bc_masks[i]:
            val >>= bc_shifts[i]
            r |= bc_shifts[i]

    return r

def hex_string(val):
    if not val:
        return None

    buf = ""

    for b in val:
        if b <= 0x0F:
            buf += '0'
        buf += hex(b)[2:]

    return buf

#TODO: Maybe move this to db.py and make it use cursor if in PostgreSQL mode.
def page_query(query, page_size=10):
    "Batch fetch an SQLAlchemy query."

    offset = 0

    while True:
        page = query.limit(page_size).offset(offset).all()

        for row in page:
            yield row

        if len(page) < page_size:
            break

        offset += page_size

def decode_key(encoded):
    #assert chord.NODE_ID_BITS == 512

    significant_bits = None

    kl = len(encoded)

    if kl == 128:
        data_key = bytes.fromhex(encoded)
    elif kl in (103, 102):
        data_key = bytes(mbase32.decode(encoded))
    else:
        data_key = mbase32.decode(encoded, False)
        significant_bits = 5 * kl

    return data_key, significant_bits
