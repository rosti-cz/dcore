# /etc/profile sources this for POSIX login shells; keep remote commands quiet.
case $- in
    *i*)
        if [ -t 1 ] && [ ! -e "$HOME/.hushlogin" ]; then
            /usr/bin/fastfetch || true
        fi
        ;;
esac
