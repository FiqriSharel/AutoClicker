# AutoClicker

AutoClicker is a Windows desktop app for repeating key presses and mouse actions. Use it to loop a key sequence, click at a set interval, or record and replay a mouse macro.

## Download and run

1. Download [AutoClicker-windows-x64.zip](./AutoClicker-windows-x64.zip) from this repository. On GitHub, open the file and click **Download raw file**.
2. Extract the whole ZIP and open the `AutoClicker` folder.
3. Double-click `AutoClicker.exe`. Keep the `_internal` folder beside it.

The portable build targets 64-bit Windows 10 and 11. It includes Python and its dependencies. The executable is unsigned.

## Quick start

1. Open **Key sequence** and enter the first key. Use **Add key** to enter more, or **Remove** next to any key to take it out.
2. Set the interval in milliseconds. The default is 40 ms between presses.
3. Focus the target app and press **`=`** to start. Press it again to stop. Key sequences also stop when you switch windows.

To change the shortcut, click the current key under **Start / stop hotkey** and press your new key or combination. The default recording shortcut is **`-`**. Changes are saved in your Windows profile.

| Mode | What it does |
| --- | --- |
| Key sequence | Loops one or more keys in order. |
| Rapid click | Repeats a left-click at your cursor. |
| Click and hold | Alternates between holding and releasing the left mouse button. |
| Record macro | Records mouse movements and clicks for playback. |
| Background | Sends clicks or a recorded mouse loop to a selected window. Use **Test one click** first because support depends on how the target app handles input. |

## Run from source

Use 64-bit [Python 3.14](https://www.python.org/downloads/) on Windows. From the project folder in PowerShell, run:

```powershell
py -3.14 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe autoclickerrecord.py
```

## Build a Windows release

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-build.txt
.\.venv\Scripts\python.exe build_release.py
```

The build writes `dist/AutoClicker-windows-x64.zip`. Extract it and check that the app launches before publishing. Commit the source, README, and build files to GitHub. Upload the ZIP as an asset on a [GitHub Release](https://github.com/FiqriSharel/AutoClicker/releases/new).
