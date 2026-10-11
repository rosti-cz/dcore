# Static shell-independent paths: do not execute user-managed brew as root.
case ":${PATH-}:" in
    *:/home/linuxbrew/.linuxbrew/sbin:*) ;;
    *) PATH="/home/linuxbrew/.linuxbrew/sbin:${PATH-}" ;;
esac
case ":${PATH-}:" in
    *:/home/linuxbrew/.linuxbrew/bin:*) ;;
    *) PATH="/home/linuxbrew/.linuxbrew/bin:${PATH-}" ;;
esac
export PATH
export HOMEBREW_PREFIX=/home/linuxbrew/.linuxbrew
export HOMEBREW_CELLAR=/home/linuxbrew/.linuxbrew/Cellar
export HOMEBREW_REPOSITORY=/home/linuxbrew/.linuxbrew/Homebrew
# sudo/su sessions may lack pam_systemd's runtime variable; linger creates it.
if [ "$(id -un)" = core ] && [ -z "${XDG_RUNTIME_DIR-}" ]; then
    if [ -d "/run/user/$(id -u)" ]; then
        XDG_RUNTIME_DIR="/run/user/$(id -u)"
        export XDG_RUNTIME_DIR
    fi
fi
