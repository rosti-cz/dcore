# Show branding once at an interactive login, including SSH and local consoles.
if status is-interactive; and status is-login; and test -t 1; and not test -e "$HOME/.hushlogin"
    /usr/bin/fastfetch; or true
end
