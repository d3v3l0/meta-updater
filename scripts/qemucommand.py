from os.path import exists, isdir, join, realpath, abspath
from os import listdir
import random
import socket
from shutil import copyfile
from subprocess import check_output

EXTENSIONS = {
    'intel-corei7-64': 'wic',
    'qemux86-64': 'ota-ext4'
}


def find_local_port(start_port):
    """"
    Find the next free TCP port after 'start_port'.
    """

    for port in range(start_port, start_port + 10):
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.bind(('', port))
            return port
        except socket.error:
            print("Skipping port %d" % port)
        finally:
            s.close()
    raise Exception("Could not find a free TCP port")


def random_mac():
    """Return a random Ethernet MAC address
    @link https://www.iana.org/assignments/ethernet-numbers/ethernet-numbers.xhtml#ethernet-numbers-2
    """
    hex_digits = '0123456789abcdef'
    tail = ':'.join([random.choice(hex_digits) + random.choice(hex_digits) for _ in range(4)])
    return f"ca:fe:{tail}"


class QemuCommand(object):
    def __init__(self, args):
        self.enable_u_boot = True
        self.dry_run = args.dry_run
        self.overlay = args.overlay
        self.host_fwd = None
        self.kernel = None
        self.drive_interface = "ide"

        if hasattr(args, 'uboot_enable'):
            self.enable_u_boot = args.uboot_enable.lower() in ("yes", "true", "1")

        # Rise an exception if U-Boot is disabled and overlay option is used
        if not self.enable_u_boot and self.overlay:
            raise EnvironmentError("An overlay option is currently supported only with U-Boot loader!")

        # If booting with u-boot is disabled we use "ext4" root fs instead of custom one "ota-ext4"
        if not self.enable_u_boot:
            self.drive_interface = "virtio"
            EXTENSIONS['qemux86-64'] = 'ext4'

        if args.machine:
            self.machine = args.machine
        else:
            if not isdir(args.dir):
                raise ValueError(
                    f"Directory {args.dir} does not exist, please specify a --machine or a valid images directory"
                )
            machines = listdir(args.dir)
            if len(machines) == 1:
                self.machine = machines[0]
            else:
                raise ValueError(
                    f"Could not autodetect machine type. More than one entry in {args.dir}. Maybe --machine qemux86-64?"
                )

        # If using an overlay with U-Boot, copy the rom when we create the
        # overlay so that we can keep it around just in case.
        if args.efi:
            self.bios = 'OVMF.fd'
        elif self.enable_u_boot:
            if args.bootloader:
                uboot_path = args.bootloader
            else:
                uboot_path = abspath(join(args.dir, self.machine, 'u-boot-qemux86-64.rom'))
            if self.overlay:
                new_uboot_path = f'{self.overlay}.u-boot.rom'
                if not exists(self.overlay):
                    if not exists(uboot_path):
                        raise ValueError(f"U-Boot image {uboot_path} does not exist")
                    if not exists(new_uboot_path):
                        if self.dry_run:
                            print(f"cp {uboot_path} {new_uboot_path}")
                        else:
                            copyfile(uboot_path, new_uboot_path)
                uboot_path = new_uboot_path
            if not exists(uboot_path) and (
                not self.dry_run or exists(self.overlay)
            ):
                raise ValueError(f"U-Boot image {uboot_path} does not exist")
            self.bios = uboot_path
        else:
            self.kernel = abspath(join(args.dir, self.machine, 'bzImage-qemux86-64.bin'))

        # If using an overlay, we need to keep the "backing" image around, as
        # bitbake will often clean it up, and the overlay silently depends on
        # the hardcoded path. The easiest solution is to keep the file and use
        # a relative path to it.
        if exists(args.imagename):
            image = realpath(args.imagename)
        else:
            ext = EXTENSIONS.get(self.machine, 'wic')
            image = join(args.dir, self.machine, f'{args.imagename}-{self.machine}.{ext}')
        if self.overlay:
            new_image_path = f'{self.overlay}.img'
            if not exists(self.overlay):
                if not exists(image):
                    raise ValueError(f"OS image {image} does not exist")
                if not exists(new_image_path):
                    if self.dry_run:
                        print(f"cp {image} {new_image_path}")
                    else:
                        copyfile(image, new_image_path)
            self.image = new_image_path
        else:
            self.image = realpath(image)
        if not exists(self.image) and (not self.dry_run or exists(self.overlay)):
            raise ValueError(f"OS image {self.image} does not exist")

        self.mac_address = args.mac if args.mac else random_mac()
        self.serial_port = find_local_port(8990)
        self.ssh_port = find_local_port(2222)
        self.mem = args.mem if args.mem else "1G"
        if args.kvm is None:
            # Autodetect KVM using 'kvm-ok'
            try:
                check_output(['kvm-ok'])
                self.kvm = True
            except Exception:
                self.kvm = False
        else:
            self.kvm = args.kvm
        self.gui = not args.no_gui
        self.gdb = args.gdb
        self.pcap = args.pcap
        self.secondary_network = args.secondary_network

        # Append additional port forwarding to QEMU command line.
        if hasattr(args, 'host_forward'):
            self.host_fwd = args.host_forward

    def command_line(self):
        netuser = 'user,hostfwd=tcp:0.0.0.0:%d-:22,restrict=off' % self.ssh_port
        if self.gdb:
            netuser += ',hostfwd=tcp:0.0.0.0:2159-:2159'
        if self.host_fwd:
            netuser += f",hostfwd={self.host_fwd}"

        cmdline = [
            "qemu-system-x86_64",
        ]
        if self.enable_u_boot:
            cmdline += ["-bios", self.bios]
        else:
            cmdline += ["-kernel", self.kernel]

        if not self.overlay:
            cmdline += [
                "-drive",
                f"file={self.image},if={self.drive_interface},format=raw,snapshot=on",
            ]
        cmdline += [
            "-serial",
            "tcp:127.0.0.1:%d,server,nowait" % self.serial_port,
            "-m",
            self.mem,
            "-object",
            "rng-random,id=rng0,filename=/dev/urandom",
            "-device",
            "virtio-rng-pci,rng=rng0",
            "-net",
            netuser,
            "-net",
            f"nic,macaddr={self.mac_address}",
        ]
        if self.pcap:
            cmdline += ['-net', f'dump,file={self.pcap}']
        if self.secondary_network:
            cmdline += [
                '-netdev',
                'socket,id=vlan1,mcast=230.0.0.1:1234,localaddr=127.0.0.1',
                '-device',
                f'e1000,netdev=vlan1,mac={random_mac()}',
            ]
        if self.gui:
            cmdline += [
                    "-usb",
                    "-device", "usb-tablet",
                    "-show-cursor",
                    "-vga", "std"
            ]
        else:
            cmdline += [
                    "-nographic",
                    "-monitor", "null",
            ]
        cmdline += ['-enable-kvm', '-cpu', 'host'] if self.kvm else ['-cpu', 'Haswell']
        if self.overlay:
            cmdline.append(self.overlay)

        # If booting with u-boot is disabled, add kernel command line arguments through qemu -append option
        if not self.enable_u_boot:
            cmdline += ["-append", "root=/dev/vda rw highres=off console=ttyS0 ip=dhcp"]
        return cmdline

    def img_command_line(self):
        return [
            "qemu-img",
            "create",
            "-o",
            f"backing_file={self.image}",
            "-f",
            "qcow2",
            self.overlay,
        ]
