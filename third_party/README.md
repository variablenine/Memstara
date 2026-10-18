# Files from other projects

Memstara ships one thing it didn't make: the Monero Research Lab's spy node
list. It's kept exactly as published, in its own folder, so anyone can check
it against the original. Memstara only reads it. It never changes it, never
downloads a newer one and never hands it to `monerod`.

Memstara's GPL-3.0 license doesn't cover these files.

## mrl-ban-list/

| | |
|---|---|
| What | The spy node list: addresses and `/24` ranges of nodes found behaving in ways normal Monero nodes don't. Version 2 (2025-12-10), 431 entries, 14 of them ranges. |
| From | <https://github.com/Boog900/monero-ban-list> |
| Commit | `22ddf399a8dc7cbfc33519b2affafaca31b2510b` (2026-01-24) |
| Copied | `ban_list.txt` and the five files in `sigs/`, byte for byte. The project's own README isn't copied. |
| Used by | `memstara/spylist.py`, which reads `ban_list.txt` once at start |

    07b136bb548b497e8125b48a2a501e933f50a43135cfc605e6e1935b45e5e407  ban_list.txt
    baaac861a8d5279e749f79fe514fb82916cd38fd42664a102cdd3c09d8de31aa  sigs/Rucknium.sig
    a666b76b67e4220a043ac983ee8503cf0f1620dec15b3c4800847e9cc751d8f8  sigs/boog900.sig
    9ddc84b369e05d9b78659f1069c25e3cdd9baae1ee523707f4c4d5ee240898bc  sigs/hinto-janai.sig
    2828dc8b77081664e0227a780a0592328f64338f88c0dac7d7adbed6422e7435  sigs/jeffro256.sig
    cc693f07ed2e91181daf0289d5f5aa6ed567cb6e9e9d95dd6ad8f111fe6b065a  sigs/syntheticbird.sig

`monerod` started with `--enable-dns-blocklist` bans the same 431 entries: the
DNS blocklist it reads (`blocklist.moneropulse.*`) held exactly this list on
2026-10-18. It reads it on mainnet only, and with `--proxy` only alongside
`--proxy-allow-dns-leaks`; `--ban-list` with this file works either way.

### Check that it's the same file

From the Memstara folder, these print the hashes above:

    (cd third_party/mrl-ban-list && sha256sum ban_list.txt sigs/*.sig)

To compare them with the original, fetch it at the same commit and diff the
two folders. `diff` prints nothing when they match:

    git clone https://github.com/Boog900/monero-ban-list /tmp/monero-ban-list
    git -C /tmp/monero-ban-list checkout 22ddf399a8dc7cbfc33519b2affafaca31b2510b
    diff /tmp/monero-ban-list/ban_list.txt third_party/mrl-ban-list/ban_list.txt
    diff -r /tmp/monero-ban-list/sigs third_party/mrl-ban-list/sigs

Memstara checks the first hash itself every time it starts
(`SHA256` in `memstara/spylist.py`). If `ban_list.txt` differs from it by a
single byte, Memstara doesn't use the list at all, and the SPY NODE LIST lamp
reads LIST CHANGED. The tests (`tests/test_spylist.py`) fail if any file in
this folder changes, or if one is added.

### Check the signatures

Five people signed this `ban_list.txt`. Get their public keys from the places
the list's own README names, check each fingerprint, import the keys and verify:

| Signed by | Key fingerprint |
|---|---|
| boog900 | `37AA 6F0F 4776 A897 EEA4 4E5C AD8B 0A2C F759 9219` (it signs with its subkey `A875 F544 CB56 9CB9 6889 791E 42AB 1287 CB00 41C2`) |
| jeffro256 | `2EAA C930 E6B9 0CB0 19C0 1E80 6F79 797A 6E39 2442` |
| Rucknium | `5D5E 08A2 389D F225 ABD0 781B 291E 0C22 9D63 1697` |
| SyntheticBird | `5671 0E34 341A FB70 7010 17E8 A5FC 94A2 41F4 060C` |
| hinto-janai | `31C5 145A AFA5 A8DF 1C1D B2A6 D47C E05F A175 A499` |

    gpg --import their-key.asc
    gpg --verify third_party/mrl-ban-list/sigs/boog900.sig third_party/mrl-ban-list/ban_list.txt

Each should say "Good signature" (gpg adds a warning that you haven't marked
the key as trusted yourself, which is normal).

jeffro256's key in the place the README links (`utils/gpg_keys/jeffro256.asc`
in Monero's repository, at commit `004ead1a`) expired on 2025-05-14, before he
signed, so gpg says "Good signature" but also "This key has expired". The same
key in Monero's repository today (`utils/gpg_keys/jeffro256.asc` on `master`)
was extended to 2027-05-09. With that copy the "This key has expired" note
goes away, and gpg shows only the same trust warning as for the other keys.

### License

The list's repository doesn't state a license. It's published for every
Monero node to use, and `monerod`'s DNS blocklist serves the same entries.
It's included here unchanged and with credit, and would be removed if its
authors asked.

### Updating it

A new version of the list means: copy the new files over these from a
specific commit, change the commit and hashes here, `SHA256`, `VERSION` and
`DATE` in `memstara/spylist.py` and the hashes in `tests/test_spylist.py`, and
say so in the commit message. Nothing updates it automatically.
