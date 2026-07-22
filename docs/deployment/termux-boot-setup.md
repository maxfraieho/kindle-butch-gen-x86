# Termux:Boot setup (device-reboot autostart)

`deploy.sh` (with `-a`/`--autostart`) already configures two things automatically:

1. `~/.bashrc` autostart — fires whenever a **new Termux shell session starts**
   (i.e. you manually open/reopen the Termux app). Sufficient to recover from a
   Termux crash you notice and respond to by reopening the app — confirmed
   working live in production: a real Termux crash mid-conversion, on manual
   restart the interrupted book resumed automatically with zero manual steps.
2. `~/.termux/boot/start-services.sh` — the same startup sequence, ready to be
   triggered by a genuine Android **device boot**, independent of you ever
   opening Termux yourself.

Step 2 only actually runs if the separate **Termux:Boot** plugin app is
installed — `deploy.sh` writes the script but cannot install an APK for you
(Android does not allow silent app installation, even from within Termux).

## One-time manual step

1. Install Termux:Boot from **the same source as your Termux app itself**:
   - F-Droid: <https://f-droid.org/packages/com.termux.boot/>
   - Installing Termux:Boot from a different source than Termux itself (e.g.
     Termux from GitHub releases but Termux:Boot from F-Droid, or vice versa)
     is a known-unreliable combination — Android package signing must match
     between an app and its plugins.
2. Open the Termux:Boot app **once** after installing it. This is what grants
   it Android's `RECEIVE_BOOT_COMPLETED` permission — without opening it at
   least once, the boot script will never actually run.
3. That's it. `~/.termux/boot/start-services.sh` is already in place
   (written by `deploy.sh`) and will run automatically on every future device
   boot from this point on.

## Verifying it worked

After a real device reboot (not just reopening Termux):

```bash
ps aux | grep -E "sshd|llama-server|kbg_web/app.py"
```

All three should be running within a minute or two of boot, without you
having opened the Termux app manually. If a book conversion was interrupted
by whatever caused the reboot, check `~/kbg-autoresume.log` for confirmation
it was picked back up.

## Why both triggers exist, not just one

`~/.bashrc` and `~/.termux/boot/start-services.sh` are two *independent*
triggers for the exact same underlying script
(`kindle-butch-gen/bin/start-all-services.sh`) rather than duplicated logic:

- `.bashrc` alone: works when you notice a crash and reopen Termux yourself,
  but does nothing if the device reboots while you're not looking (e.g.
  overnight, or Android's own background process killer taking Termux down
  without you noticing).
- Termux:Boot alone: works on a genuine device reboot, but Android does not
  reliably fire `BOOT_COMPLETED` for every kind of app/process restart Termux
  itself might experience (a plain app crash that doesn't reboot the whole
  device, for instance).

Both together cover the realistic set of "something went down and needs to
come back up" scenarios for this project without needing to guess which one
will happen.
