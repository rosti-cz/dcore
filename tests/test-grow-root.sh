#!/usr/bin/env bash
# Run in a privileged container with /dev mounted, never on a mounted host root.
set -euo pipefail
[[ $EUID -eq 0 ]] || { echo 'Run as root in a disposable privileged container.' >&2; exit 2; }
if mountpoint -q /sysroot; then
    echo 'Refusing to test with an existing /sysroot mount.' >&2
    exit 2
fi
helper=${GROW_ROOT_HELPER:-/usr/libexec/dcore/grow-root}
work=$(mktemp -d /tmp/dcore-grow-tests.XXXXXXXX)
loop=; second_loop=; mapper=; mounted=false
cleanup_fixture() {
    if [[ $mounted == true ]]; then umount /sysroot; mounted=false; fi
    if [[ -n $mapper ]]; then cryptsetup close "$mapper"; mapper=; fi
    if [[ -n $second_loop ]]; then losetup --detach "$second_loop"; second_loop=; fi
    if [[ -n $loop ]]; then losetup --detach "$loop"; loop=; fi
}
cleanup() {
    cleanup_fixture
    rm -rf -- "$work"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
capacity() { stat -f -c '%b' /sysroot; }
make_fixture() {
    local filesystem=$1 suffix=$2
    cleanup_fixture
    raw="$work/$suffix.raw"
    truncate -s 1G "$raw"
    loop=$(losetup --find --show --partscan "$raw")
    sfdisk "$loop" > /dev/null <<'PARTITIONS'
label: gpt
size=64M, type=linux, name=preserve
type=linux, name=root
PARTITIONS
    partx --update "$loop"
    udevadm settle
    case $filesystem in
        ext4) mkfs.ext4 -q "${loop}p2" ;;
        btrfs) mkfs.btrfs -q "${loop}p2" ;;
        xfs) mkfs.xfs -q "${loop}p2" ;;
    esac
    mount "${loop}p2" /sysroot
    mounted=true
    printf 'data survives root growth\n' > /sysroot/payload
    printf 'unrelated partition remains intact\n' | dd of="${loop}p1" conv=notrunc status=none
    preserved=$(sha256sum "${loop}p1" | cut -d' ' -f1)
}
assert_preserved() {
    grep -qx 'data survives root growth' /sysroot/payload
    [[ $(sha256sum "${loop}p1" | cut -d' ' -f1) == "$preserved" ]]
}
for filesystem in ext4 btrfs xfs; do
    make_fixture "$filesystem" "$filesystem"
    initial=$(capacity)
    truncate -s 2G "$raw"
    losetup --set-capacity "$loop"
    original_table=$(sfdisk --dump "$loop")
    "$helper" --dry-run
    [[ $(sfdisk --dump "$loop") == "$original_table" ]]
    "$helper"
    grown=$(capacity)
    [[ $grown -gt $initial ]]
    assert_preserved
    grown_table=$(sfdisk --dump "$loop")
    "$helper"
    [[ $(capacity) == "$grown" && $(sfdisk --dump "$loop") == "$grown_table" ]]
    # Simulate interruption after partition enlargement, before filesystem growth.
    truncate -s 3G "$raw"
    losetup --set-capacity "$loop"
    growpart "$loop" 2
    "$helper"
    [[ $(capacity) -gt $grown ]]
    assert_preserved
    # Grow while the original backing mount is read-only, preserving its flags.
    recovered=$(capacity)
    mount -o remount,bind,ro /sysroot
    options=$(findmnt -n -o OPTIONS --mountpoint /sysroot)
    truncate -s 4G "$raw"
    losetup --set-capacity "$loop"
    "$helper"
    [[ $(capacity) -gt $recovered ]]
    [[ $(findmnt -n -o OPTIONS --mountpoint /sysroot) == "$options" ]]
    assert_preserved
    echo "PASS: $filesystem grows, repeats safely, recovers partial growth, preserves read-only flags"
done

# LUKS2 opens at the full partition size. Grow GPT before unlocking, then
# grow the filesystem through its active mapping without another passphrase.
for filesystem in ext4 btrfs xfs; do
    make_fixture "$filesystem" "luks-$filesystem"
    umount /sysroot
    mounted=false
    printf 'test-only-growth-passphrase' | cryptsetup luksFormat --batch-mode --pbkdf pbkdf2 --key-file - "${loop}p2"
    mapper="dcore-grow-test-$RANDOM"
    printf 'test-only-growth-passphrase' | cryptsetup open --key-file - "${loop}p2" "$mapper"
    case $filesystem in
        ext4) mkfs.ext4 -q "/dev/mapper/$mapper" ;;
        btrfs) mkfs.btrfs -q "/dev/mapper/$mapper" ;;
        xfs) mkfs.xfs -q "/dev/mapper/$mapper" ;;
    esac
    mount "/dev/mapper/$mapper" /sysroot
    mounted=true
    printf 'data survives root growth\n' > /sysroot/payload
    initial=$(capacity)
    truncate -s 2G "$raw"
    losetup --set-capacity "$loop"
    unchanged=$(sfdisk --dump "$loop")
    "$helper"
    "$helper" --luks-partition "${loop}p2"
    [[ $(sfdisk --dump "$loop") == "$unchanged" ]]
    umount /sysroot
    mounted=false
    cryptsetup close "$mapper"
    "$helper" --dry-run --luks-partition "${loop}p2"
    [[ $(sfdisk --dump "$loop") == "$unchanged" ]]
    "$helper" --luks-partition "${loop}p2"
    printf 'test-only-growth-passphrase' | cryptsetup open --key-file - "${loop}p2" "$mapper"
    mount "/dev/mapper/$mapper" /sysroot
    mounted=true
    "$helper"
    grown=$(capacity)
    [[ $grown -gt $initial ]]
    assert_preserved
    grown_table=$(sfdisk --dump "$loop")
    "$helper"
    [[ $(capacity) == "$grown" && $(sfdisk --dump "$loop") == "$grown_table" ]]
    # Simulate interruption between pre-unlock partition and filesystem growth.
    umount /sysroot
    mounted=false
    cryptsetup close "$mapper"
    truncate -s 3G "$raw"
    losetup --set-capacity "$loop"
    "$helper" --luks-partition "${loop}p2"
    "$helper" --luks-partition "${loop}p2"
    printf 'test-only-growth-passphrase' | cryptsetup open --key-file - "${loop}p2" "$mapper"
    mount "/dev/mapper/$mapper" /sysroot
    mount -o remount,bind,ro /sysroot
    mounted=true
    options=$(findmnt -n -o OPTIONS --mountpoint /sysroot)
    "$helper"
    [[ $(capacity) -gt $grown && $(findmnt -n -o OPTIONS --mountpoint /sysroot) == "$options" ]]
    assert_preserved
    echo "PASS: LUKS2 $filesystem grows after unlock, recovers partial growth, preserves data and read-only flags"
done

# A root followed by another partition must not consume even a free gap.
make_fixture ext4 non-final
cleanup_fixture
sfdisk "$raw" > /dev/null <<'PARTITIONS'
label: gpt
size=64M, type=linux, name=preserve
size=512M, type=linux, name=root
size=64M, type=linux, name=later
PARTITIONS
loop=$(losetup --find --show --partscan "$raw")
udevadm settle
mkfs.ext4 -q "${loop}p2"
mount "${loop}p2" /sysroot
mounted=true
truncate -s 4G "$raw"
losetup --set-capacity "$loop"
unchanged=$(sfdisk --dump "$loop")
"$helper"
[[ $(sfdisk --dump "$loop") == "$unchanged" ]]
echo 'PASS: root followed by another partition is unchanged'
# The same non-final layout must remain untouched before and after LUKS unlock.
umount /sysroot
mounted=false
printf 'test-only-growth-passphrase' | cryptsetup luksFormat --batch-mode --pbkdf pbkdf2 --key-file - "${loop}p2"
"$helper" --luks-partition "${loop}p2"
[[ $(sfdisk --dump "$loop") == "$unchanged" ]]
mapper="dcore-grow-test-$RANDOM"
printf 'test-only-growth-passphrase' | cryptsetup open --key-file - "${loop}p2" "$mapper"
mkfs.ext4 -q "/dev/mapper/$mapper"
mount "/dev/mapper/$mapper" /sysroot
mounted=true
"$helper"
[[ $(sfdisk --dump "$loop") == "$unchanged" ]]
echo 'PASS: non-final LUKS2 root skipped before and after unlock'


# Partition numbering is not physical ordering: root p1 ends after data p2.
cleanup_fixture
raw="$work/out-of-order.raw"
truncate -s 1G "$raw"
loop=$(losetup --find --show --partscan "$raw")
sfdisk "$loop" > /dev/null <<'PARTITIONS'
label: gpt
start=131072, type=linux, name=root
start=2048, size=129024, type=linux, name=preserve
PARTITIONS
partx --update "$loop"
udevadm settle
mkfs.ext4 -q "${loop}p1"
mount "${loop}p1" /sysroot
mounted=true
initial=$(capacity)
truncate -s 2G "$raw"
losetup --set-capacity "$loop"
"$helper"
[[ $(capacity) -gt $initial ]]
echo 'PASS: physically final root grows despite a lower partition number'

# Read-only mount flags must survive an invocation.
mount -o remount,bind,ro /sysroot
old_options=$(findmnt -n -o OPTIONS --mountpoint /sysroot)
truncate -s 3G "$raw"
losetup --set-capacity "$loop"
"$helper"
[[ $(findmnt -n -o OPTIONS --mountpoint /sysroot) == "$old_options" ]]
echo 'PASS: original read-only mount flags preserved'

# A multi-device Btrfs root must be rejected before editing its partition table.
make_fixture btrfs multi-device
umount /sysroot
mounted=false
truncate -s 1G "$work/second.raw"
second_loop=$(losetup --find --show "$work/second.raw")
mkfs.btrfs -q -f "${loop}p2" "$second_loop"
mount "${loop}p2" /sysroot
mounted=true
truncate -s 2G "$raw"
losetup --set-capacity "$loop"
unchanged=$(sfdisk --dump "$loop")
"$helper"
[[ $(sfdisk --dump "$loop") == "$unchanged" ]]
echo 'PASS: multi-device Btrfs is unchanged'

# Whole-device filesystems and unsupported mount types must not edit disks.
cleanup_fixture
raw="$work/whole-device.raw"
truncate -s 256M "$raw"
loop=$(losetup --find --show "$raw")
mkfs.ext4 -q "$loop"
mount "$loop" /sysroot
mounted=true
"$helper"
echo 'PASS: whole-device filesystem skipped'
cleanup_fixture
mount -t tmpfs tmpfs /sysroot
mounted=true
printf 'preserved\n' > /sysroot/payload
"$helper"
grep -qx preserved /sysroot/payload
echo 'PASS: unsupported filesystem skipped'

echo 'All root growth integration tests passed.'

