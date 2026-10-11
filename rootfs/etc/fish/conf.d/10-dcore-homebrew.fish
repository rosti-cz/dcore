fish_add_path --global --path /home/linuxbrew/.linuxbrew/bin /home/linuxbrew/.linuxbrew/sbin
set -gx HOMEBREW_PREFIX /home/linuxbrew/.linuxbrew
set -gx HOMEBREW_CELLAR /home/linuxbrew/.linuxbrew/Cellar
set -gx HOMEBREW_REPOSITORY /home/linuxbrew/.linuxbrew/Homebrew
if test (id -un) = core; and not set -q XDG_RUNTIME_DIR
    if test -d /run/user/(id -u)
        set -gx XDG_RUNTIME_DIR /run/user/(id -u)
    end
end
