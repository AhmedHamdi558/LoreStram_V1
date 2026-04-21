<div align="center">

# AndroidStream

**Turn your Android phone into a live virtual camera for your PC.**

Stream your phone screen — or camera — directly into OBS, Zoom, Teams, or any other application that accepts a webcam input. No root required.

---

```
Android Device  ──USB──►  ADB Port Forward  ──TCP──►  AndroidStream  ──►  Virtual Camera
```

</div>

---

## How It Works

The system has two parts that work together:

| Part | What it does |
|---|---|
| **Android APK** | Captures the screen or camera, encodes each frame as JPEG, and streams it over a local TCP socket |
| **PC Application** | Receives the JPEG frames, decodes them, displays a live preview, and feeds them into a virtual camera driver |

The two parts talk through **ADB port forwarding**, which routes TCP traffic from the PC through the USB cable to the phone without needing Wi-Fi or any network configuration.

---

## Requirements

### On the PC

- **Python 3.10 or newer** — [python.org/downloads](https://www.python.org/downloads/)
- **Android Platform Tools** (for the `adb` command) — [Download here](https://developer.android.com/tools/releases/platform-tools)
- **OBS Studio 28 or newer** — needed to install the Virtual Camera driver on Windows — [obsproject.com](https://obsproject.com)

### On the Phone

- Android 7.0 or newer
- USB Debugging enabled
- The **AndroidStream APK** installed

---

## Step-by-Step Setup Guide

### Step 1 — Install ADB on your PC

1. Download **Android Platform Tools** from the link above.
2. Extract the zip file to a permanent location, for example `C:\platform-tools`.
3. Add that folder to your system PATH:
   - Press `Win + S`, search for **Environment Variables**, open it.
   - Under **System Variables**, find **Path**, click **Edit**.
   - Click **New**, paste the path to the folder (e.g. `C:\platform-tools`).
   - Click OK on all dialogs.
4. Open a new Command Prompt and run:
   ```
   adb version
   ```
   You should see a version number. If you get an error, the PATH was not set correctly.

---

### Step 2 — Install OBS (Windows only)

OBS is needed because its Virtual Camera driver is what makes your phone stream appear as a webcam to other applications.

1. Download OBS Studio from [obsproject.com](https://obsproject.com).
2. Install it and launch it once. You can close it immediately after — launching it once registers the virtual camera driver with Windows.

> **Linux users:** Run `sudo modprobe v4l2loopback` instead. No OBS needed.

---

### Step 3 — Install Python dependencies

Open a terminal or Command Prompt in the project folder and run:

```bash
pip install -r requirements.txt
```

This installs four libraries: PyQt6 (the UI), OpenCV (image decoding), pyvirtualcam (virtual camera output), and NumPy (frame buffer handling).

---

### Step 4 — Enable USB Debugging on your phone

1. Open **Settings** on your Android phone.
2. Scroll to **About Phone** and tap **Build Number** seven times. A message will confirm that Developer Options are now enabled.
3. Go back to **Settings**, open **Developer Options**, and enable **USB Debugging**.

---

### Step 5 — Install the APK

1. Connect your phone to the PC via USB.
2. On the phone, approve the USB Debugging prompt when it appears.
3. Install the APK by double-clicking it (if your phone allows it), or run:
   ```bash
   adb install AndroidStream.apk
   ```

---

### Step 6 — Run the PC application

```bash
python android_stream.py
```

The application window will open.

---

### Step 7 — Connect and stream

Follow these steps in order every time you want to use the system:

1. **Connect your phone via USB** and make sure the cable supports data transfer (not charge-only).

2. **Open the AndroidStream app on your phone** and tap **Start Broadcasting**. The app will begin listening for a connection on port 8080.

3. **In the PC application**, click **Activate ADB**. The status below the button will confirm the device was found and port forwarding is active.

4. **Click Start Stream**. The preview area will show your phone's output in real time, and the LIVE badge in the top-right corner will turn red.

5. **Open OBS** (or Zoom, Teams, etc.) and add a **Video Capture Device** source. Select **OBS Virtual Camera** from the list. Your phone's stream will appear immediately.

---

## Troubleshooting

**"No Android device detected over USB"**
- Make sure the USB cable supports data, not just charging.
- Re-enable USB Debugging in Developer Options.
- Try a different USB port on your PC.
- Run `adb devices` in a terminal — your device should appear with the status `device`.

**"Connection refused"**
- The AndroidStream APK is not running on the phone, or it is not in broadcast mode. Open the app and start broadcasting before clicking Start Stream on the PC.

**"adb not found in PATH"**
- Platform Tools are not installed, or the PATH was not set correctly. Revisit Step 1.

**Virtual camera not showing in OBS / Zoom**
- Make sure you launched OBS at least once after installing it (Step 2).
- Restart the PC application after OBS is installed.
- On Windows, try running the PC application as Administrator once.

**Poor frame rate or stuttering**
- Lower the resolution in the Stream Settings panel (try 854x480 or 640x360).
- Reduce the Target FPS to 20 or 15.
- Use a USB 3.0 port and a high-quality cable.

---

## Project Structure

```
AndroidStream/
├── android_stream.py   # PC receiver and virtual camera application
├── requirements.txt    # Python dependencies
├── AndroidStream.apk   # Android broadcaster app
└── README.md           # This file
```

---

## Wire Protocol

For anyone curious about how the two sides communicate:

Each frame is sent as a simple length-prefixed binary message:

```
[ 4 bytes: uint32 big-endian frame size ] [ N bytes: JPEG image data ]
```

The receiver reads the 4-byte header, extracts the frame size, then reads exactly that many bytes of JPEG data. This repeats continuously as long as the connection is open.

---

## License

MIT — free to use, modify, and distribute.
