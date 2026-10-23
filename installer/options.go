package main

import (
	"errors"
	"flag"
	"fmt"
	"io"
	"regexp"
	"strings"
)

//go:generate python3 ../scripts/embed-installer-assets.py

var installerVersion = "development"

type options struct {
	mode, image, operation string
	version, help          bool
}

func parseOptions(args []string, output io.Writer) (options, error) {
	o := options{mode: "live"}
	install := len(args) > 0 && args[0] == "install"
	if install {
		args = args[1:]
		o.mode = "online"
	}
	f := flag.NewFlagSet("dcore-installer", flag.ContinueOnError)
	f.SetOutput(output)
	branch := f.String("branch", "stable", "dcore image tag (install mode)")
	image := f.String("image", "", "explicit OCI image reference (install mode)")
	host := f.Bool("host", false, "install the exact booted dcore image as an ordinary user")
	f.BoolVar(&o.version, "version", false, "print installer version")
	f.StringVar(&o.operation, "internal-backend", "", "internal privileged host operation")
	f.Usage = func() {
		fmt.Fprintln(output, "Usage: dcore-installer install [--branch stable | --image OCI_REFERENCE]\n       dcore-installer --host\n       dcore-installer  (offline ISO)\n\nRun install as root on an amd64 UEFI Linux host. The destination is selected interactively.")
		f.VisitAll(func(v *flag.Flag) {
			if v.Name == "internal-backend" {
				return
			}
			fmt.Fprintf(output, "  --%s: %s", v.Name, v.Usage)
			if v.Name == "branch" {
				fmt.Fprint(output, " (default stable)")
			}
			fmt.Fprintln(output)
		})
	}
	if err := f.Parse(args); err != nil {
		if errors.Is(err, flag.ErrHelp) {
			o.help = true
			return o, nil
		}
		return o, err
	}
	if f.NArg() != 0 {
		return o, fmt.Errorf("unexpected argument: %s", f.Arg(0))
	}
	branchSet := false
	imageSet := false
	f.Visit(func(v *flag.Flag) {
		if v.Name == "branch" {
			branchSet = true
		}
		if v.Name == "image" {
			imageSet = true
		}
	})
	if install && *host {
		return o, errors.New("install and --host are mutually exclusive")
	}
	if !install && (branchSet || imageSet) {
		return o, errors.New("--branch and --image require the install command")
	}
	if branchSet && imageSet {
		return o, errors.New("use either --branch or --image")
	}
	if *host {
		o.mode = "host"
	}
	if o.operation != "" && o.mode != "host" {
		return o, errors.New("internal backend requires --host")
	}
	if install {
		if imageSet {
			o.image = *image
		} else {
			if !regexp.MustCompile(`^[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$`).MatchString(*branch) {
				return o, errors.New("invalid branch: expected an OCI image tag")
			}
			o.image = "ghcr.io/rosti-cz/dcore:" + *branch
		}
		if err := validateImage(o.image); err != nil {
			return o, err
		}
	}
	return o, nil
}

func validateImage(ref string) error {
	// Only registry references, never option-like input or alternate transports.
	if strings.Contains(ref, "://") || !regexp.MustCompile(`^[a-zA-Z0-9][a-zA-Z0-9._:/@-]*$`).MatchString(ref) || !strings.Contains(ref, "/") {
		return errors.New("invalid image: use a fully qualified OCI registry reference")
	}
	if strings.Contains(ref, "@") {
		parts := strings.Split(ref, "@")
		if len(parts) != 2 || !regexp.MustCompile(`^sha256:[a-f0-9]{64}$`).MatchString(parts[1]) {
			return errors.New("image digest must be sha256 followed by 64 lowercase hexadecimal characters")
		}
	}
	name := strings.Split(ref, "@")[0]
	if colon := strings.LastIndex(name, ":"); colon > strings.LastIndex(name, "/") {
		if !regexp.MustCompile(`^[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$`).MatchString(name[colon+1:]) {
			return errors.New("invalid OCI image tag")
		}
		name = name[:colon]
	}
	registry, repository, ok := strings.Cut(name, "/")
	if !ok || !regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9.-]*(?::[0-9]+)?$`).MatchString(registry) || (!strings.ContainsAny(registry, ".:") && registry != "localhost") {
		return errors.New("image must name a registry host, for example ghcr.io/rosti-cz/dcore:stable")
	}
	if !regexp.MustCompile(`^[a-z0-9]+(?:[._-]+[a-z0-9]+)*(?:/[a-z0-9]+(?:[._-]+[a-z0-9]+)*)*$`).MatchString(repository) {
		return errors.New("invalid OCI image repository")
	}
	return nil
}
