This image is derived from https://github.com/bootcrew/mono at commit
`5f048fa65a94daefc814d3cdd941d8d1e113c09e` (Apache-2.0).

The Debian Containerfile and all three shared scripts were used as the starting
point. Local changes select Trixie, pin bootc to a release, build composefs-capable OSTree on Trixie, select the image kernel
explicitly, preserve the Incus payload under immutable `/opt`, build ZFS, and add packages, maintenance services and CI.
`shared/bootc-rootfs.sh` preserves `/opt` as a real directory for LXC's symlink-free
rootfs mount path. The adapted files retain attribution.
The upstream copyright notice is in `LICENSES/bootcrew-mono.txt`; the complete
Apache-2.0 license is in `LICENSES/Apache-2.0.txt`.

bootc is built from https://github.com/bootc-dev/bootc; its upstream licenses
remain in the source checkout during the build. Incus uses the signed Zabbly repository
documented at https://github.com/zabbly/incus.
