# Security

Memstara only reads, but it may hold the login to your node's unrestricted
RPC port, which can change the node. A bug that lets it send anything other
than the reads on its list (`memstara/rpc.py`), that lets anything other than
its own window use it, or that sends anything to the node the screens didn't
ask for, is a security problem.

Please report security problems privately: on GitHub, open the repository's
**Security** tab and choose **Report a vulnerability**. Don't open a public
issue for them. Say what you did, what happened, and which version
(`python3 -m memstara --version`).

What's in scope:

- the app's local server (`memstara/server.py`): the window key, the host and
  origin checks, and anything another program or user on the computer can
  reach;
- the read-only RPC list and the commands built on it (`memstara/rpc.py`,
  `memstara/commands.py`);
- reading more than `monerod`'s RPC, its log file and the app's own files
  (privacy mode only looks up this computer's name, your names and your home
  folder, to hide them, and a window without its packages reads
  `/etc/os-release` to print the install command), or a log path the log
  readers should refuse (`memstara/nodelog.py`, `memstara/logevents.py`);
- privacy mode letting a real address, port, name or transaction id through to
  the window;
- anything that writes outside `~/.config/memstara`, `~/.local/state/memstara`,
  `~/.cache/memstara` and the app's own entries in
  `~/.local/share/applications` and `~/.config/autostart`.
