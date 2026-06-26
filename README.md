# MacroTool

I made this to send emails automatically because all recorders are paid or bad.

Build sequences of mouse and keyboard actions, save them, and replay them on demand.

## Features

- Build macros from scratch — Move, Click, Right Click, Double Click, Wait, Type, Press Key, Scroll
- 📍 Capture current mouse position directly in the step editor
- Drag to reorder steps
- Repeat a macro N times with a configurable start delay
- Live mouse position display in the status bar
- Macros saved automatically to `macros.json`
- Emergency stop: move mouse to top-left corner of screen to abort

## Requirements

```
pip install PyQt6 pyautogui pynput
```

Python 3.10+

## Run

```
python macro_tool.py
```
