# Security

Memstara holds the login to your node's unrestricted RPC port, which can
change the node. A bug that lets anything other than its own window use it,
or that sends anything to the node the screens didn't ask for, is a security
problem.

Please report security problems privately: on GitHub, open the repository's
**Security** tab and choose **Report a vulnerability**. Don't open a public
issue for them. Say what you did, what happened, and which version
(`python3 -m memstara --version`).

What's in scope:

- the app's local server (`memstara/server.py`): the window key, the host and
  origin checks, and anything another program or user on the computer can
  reach;
- the RPC allowlist and the confirm words (`memstara/rpc.py`,
  `memstara/commands.py`);
- privacy mode letting a real address, port, name or transaction id through to
  the window;
- anything that writes outside `~/.config/memstara`, `~/.local/state/memstara`
  and the app's own entries in `~/.local/share/applications` and
  `~/.config/autostart`.
