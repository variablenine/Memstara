# Start the app when you log in

With this on, Memstara starts in the background when you log in to
the desktop, with no window, so its history (the charts going back 24 hours)
keeps filling in while you're not looking. Opening the app from your apps
shows its window; closing the window leaves it running. **Quit** in the
window's menu (☰, top right) stops it until the next login.

It never touches `monerod` or how your node is started, and it runs as you,
only once you're logged in.

**Turn it on**, either way:

- In the app: ☰ → **Start when I log in**. A check mark means it's on.
- In a terminal on the computer running the app:

  ```bash
  cd ~/memstara && python3 -m memstara --autostart on
  ```

**Turn it off**: the same menu item again, or:

```bash
cd ~/memstara && python3 -m memstara --autostart off
```

Your desktop's own list of startup apps (Startup Applications on Ubuntu,
Autostart in KDE's settings) shows it too, as Memstara, and can switch it off
there.

**After `git pull`**: ☰ → **Quit**, then open the app again, so it runs the new
code.

**If it doesn't seem to start**: ☰ → **Quit** (if the window opens at all),
then start it in a terminal, which shows its messages:

```bash
cd ~/memstara && python3 -m memstara
```

How it works: `--autostart on` writes a desktop autostart file,
`~/.config/autostart/org.memstara.Memstara.desktop`, that runs
`python3 -m memstara --background` from the app's folder. `--autostart off`
deletes it. If you move the app's folder, run
`python3 -m memstara --install` from the new place; that rewrites this file too.

