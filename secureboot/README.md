# dcore Secure Boot signing certificate

- `dcore.crt`: public X.509 certificate in PEM format for signature verification.
- `dcore.der`: the same public certificate in DER format for enrollment.
- `SECUREBOOT_SIGNING_KEY`: GitHub Actions secret containing the corresponding
  RSA private key in PEM format. Never include it in the repository, build logs,
  container layers, ISO images, or release artifacts. Keep a protected backup
  outside the repository.

The certificate uses RSA-2048 and SHA-256, permits code signing, and expires on
October 9, 2036. Its SHA-256 fingerprint is:

```text
47:D7:48:12:A8:12:DB:01:FF:15:4C:66:45:C2:22:F8:B3:86:E4:FE:69:A9:EC:4D:17:64:FE:DC:09:D1:3C:48
```

Reuse this key across releases so installed machines retain their trust. Key
rotation requires distributing and enrolling the replacement certificate.

Published image builds mount the key with Podman's `--secret` option and sign
all DKMS modules before generating the initramfs. The private key is never
copied into a container layer. Every module signature is cryptographically
verified against this certificate as part of image verification.

The ISO uses Debian's signed shim, GRUB and kernel. Installed dcore uses Debian's
signed shim, systemd-boot and kernel, with both fallback and existing systemd
entry paths routed through shim. Bootloader updates preserve these paths.

Use **Enroll key** after installation or `djust enroll-secureboot` on the target
machine. Choose a temporary enrollment password, reboot, and confirm in
MokManager. Enrollment is needed for dcore's ZFS modules; Debian's boot chain
can boot without it. The EFI partition also contains `EFI/dcore/dcore.der`.

Local unsigned development builds explicitly set `SECUREBOOT_REQUIRED=0`.
Release artifact builders reject images with unsigned dcore modules.
The external initramfs, boot arguments and composefs root digest are not covered
by a signed UKI; this implements Secure Boot compatibility, not a sealed system.
