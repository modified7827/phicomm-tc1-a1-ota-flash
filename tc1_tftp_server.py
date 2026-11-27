import argparse
import socket
import struct
import sys
import time
from pathlib import Path

PORT = 69
FILENAME = b"mico_ota.bin"
BLOCK_SIZE = 512
RETRIES_PER_BLOCK = 5
ACK_TIMEOUT_SECONDS = 3
PROGRESS_EVERY_BLOCKS = 100


def log(message: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {message}", flush=True)


def error_packet(code: int, message: bytes) -> bytes:
    return struct.pack("!HH", 5, code) + message + b"\0"


def request_details(packet: bytes) -> tuple[bytes, bytes] | None:
    if len(packet) < 4:
        return None
    opcode = struct.unpack("!H", packet[:2])[0]
    fields = packet[2:].split(b"\0")
    if len(fields) < 2 or not fields[0] or not fields[1]:
        return None
    return opcode.to_bytes(2, "big"), fields[0], fields[1].lower()


def transfer_firmware(host: str, client: tuple[str, int], firmware: bytes) -> None:
    total_blocks = (len(firmware) + BLOCK_SIZE - 1) // BLOCK_SIZE
    log(f"RRQ accepted from {client[0]}:{client[1]}; {len(firmware)} B in {total_blocks} blocks")

    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as transfer:
        transfer.bind((host, 0))
        local_port = transfer.getsockname()[1]
        transfer.settimeout(ACK_TIMEOUT_SECONDS)
        log(f"Transfer socket bound to {host}:{local_port}")

        for block in range(1, total_blocks + 1):
            offset = (block - 1) * BLOCK_SIZE
            payload = firmware[offset : offset + BLOCK_SIZE]
            packet = struct.pack("!HH", 3, block) + payload

            for attempt in range(1, RETRIES_PER_BLOCK + 1):
                transfer.sendto(packet, client)
                try:
                    acknowledgement, sender = transfer.recvfrom(516)
                except socket.timeout:
                    log(f"Block {block}/{total_blocks}: ACK timeout ({attempt}/{RETRIES_PER_BLOCK})")
                    continue

                expected_ack = struct.pack("!HH", 4, block)
                if sender == client and acknowledgement == expected_ack:
                    break

                log(
                    f"Block {block}/{total_blocks}: ignored unexpected packet "
                    f"from {sender[0]}:{sender[1]} ({len(acknowledgement)} B)"
                )
            else:
                log(f"TRANSFER FAILED: block {block}/{total_blocks} was not acknowledged")
                return

            if block == 1 or block % PROGRESS_EVERY_BLOCKS == 0 or block == total_blocks:
                sent = min(block * BLOCK_SIZE, len(firmware))
                log(f"Progress: block {block}/{total_blocks}, {sent}/{len(firmware)} B acknowledged")

    log(f"TRANSFER COMPLETE: {len(firmware)} B sent to {client[0]}:{client[1]}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Read-only TC1 TFTP firmware server")
    parser.add_argument("--host", default="10.0.0.2", help="Local IPv4 address to bind (default: 10.0.0.2)")
    args = parser.parse_args()

    firmware_path = Path(__file__).with_name(FILENAME.decode())
    try:
        firmware = firmware_path.read_bytes()
    except OSError as error:
        log(f"STARTUP FAILED: cannot read {firmware_path}: {error}")
        return 1

    if len(firmware) != 649344:
        log(f"STARTUP FAILED: {firmware_path.name} is {len(firmware)} B; expected 649344 B")
        return 1

    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as server:
            server.bind((args.host, PORT))
            log(f"READY: UDP {args.host}:{PORT}; file={firmware_path.name}; size={len(firmware)} B")
            log("Waiting for RRQ mico_ota.bin in octet mode. Press Ctrl+C to stop.")

            while True:
                packet, client = server.recvfrom(2048)
                details = request_details(packet)
                if details is None:
                    log(f"Rejected malformed request from {client[0]}:{client[1]} ({len(packet)} B)")
                    server.sendto(error_packet(4, b"malformed request"), client)
                    continue

                opcode, filename, mode = details
                if opcode != b"\0\1":
                    log(f"Rejected non-RRQ request from {client[0]}:{client[1]}")
                    server.sendto(error_packet(4, b"read requests only"), client)
                    continue
                if filename != FILENAME or mode != b"octet":
                    log(
                        f"Rejected request from {client[0]}:{client[1]}: "
                        f"filename={filename!r}, mode={mode!r}"
                    )
                    server.sendto(error_packet(1, b"only mico_ota.bin octet"), client)
                    continue

                transfer_firmware(args.host, client, firmware)
    except OSError as error:
        log(f"STARTUP FAILED: cannot bind UDP {args.host}:{PORT}: {error}")
        log(f"Check that {args.host}/8 is assigned to the wired adapter and that no other TFTP server uses UDP 69.")
        return 1
    except KeyboardInterrupt:
        log("Stopped by user")
        return 0


if __name__ == "__main__":
    sys.exit(main())
