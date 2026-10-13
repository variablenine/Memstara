# Add-ons

An add-on is one folder here, `addons/<name>/`, for something only one setup
has: a file a script of yours writes, a service on your network, a check you
run yourself. Memstara ships with none. Git ignores everything in this folder
except this guide, so an add-on made for your own setup never lands in a
commit by accident.

Deleting an add-on's folder and restarting the app removes it completely.
Settings → its own section can turn it off without deleting it, and
`python3 -m memstara --no-addons` starts with none loaded. `--addons DIR`
loads them from another folder instead.

## The smallest one

`addons/hello/addon.py`:

```python
from memstara.addons import Addon
from memstara.settings import entry


class Hello(Addon):
    name = "hello"
    title = "Hello"

    def settings(self):
        return [entry("hello.word", "text", "HELLO", "Lamp text", "What the lamp says.")]

    def poll(self, settings, now, ctx):
        return {"word": settings["hello.word"]}

    def lamps(self, data, ctx):
        return [{"id": "word", "label": "HELLO", "state": "ok", "value": data["word"],
                 "help": "Shows the word from Settings → Hello.", "screen": "status"}]


ADDON = Hello()
```

Every hook, what it returns and where that shows up (lamps, alerts, boxes in
the route diagram, notes on commands, addresses `ban` must never cover, log
files for the command drawer, a panel of its own) is in
[docs/DESIGN.md](../docs/DESIGN.md), "Core and add-ons". The core gives an
add-on nothing that changes the node. But an add-on is Python code that runs
inside the app with your user's rights, so only use add-ons you have read or
written yourself.

Give your add-on's folder a `README.md` saying what it reads and how to set it
up.
