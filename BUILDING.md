## Building from Source

This project provides GitHub Actions so you can compile and package the software yourself easily.  The output is a **portable ZIP** – no installation or admin rights required.

### Steps

1. [Fork the repo.](https://github.com/leapbtw/uxplay-windows/fork)
2. In the **Actions** tab of your fork, select **build uxplay-windows** and run it.

### What the workflow produces

| Artifact | Description |
|---|---|
| `uxplay-windows-portable` | A ZIP containing `uxplay-windows.exe` and all dependencies. Extract anywhere and run – no installation needed. |

### Portable usage (no admin rights required)

1. Download the `uxplay-windows-portable` artifact from the completed workflow run.
2. Extract the ZIP to any folder (e.g. a USB drive, `Downloads`, etc.).
3. Run `uxplay-windows.exe`.

The tray app will automatically start a bundled `mDNSResponder.exe` for AirPlay device discovery if the Bonjour service is not already installed on the machine.  All user data (arguments, log) is stored in `%APPDATA%\uxplay-windows`, which never requires admin rights.
