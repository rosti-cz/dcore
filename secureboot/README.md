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

Signing and enrollment support still need to be implemented. Adding this key
does not enable Secure Boot in existing images; keep Secure Boot disabled until
the complete boot chain and ZFS module signatures have been validated.
