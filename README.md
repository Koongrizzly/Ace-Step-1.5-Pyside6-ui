# 🎛️ Ace-Step 1.5 PySide6 UI
A different **Windows** look for **Ace Step 1.5** 🪟✨

<p align="center">

| 🚀 Quick links | |
|---|---|
| 🧩 Install | [Install notes](#-install-notes) |
| ✨ Features | [Features](#-features) |
| 🚫 Not working | [Not working / not installed](#-not-working--not-installed) |
| 🛠️ Todo | [Todo](#-todo) |

</p>

🔗 **All credits to & original model here:** https://github.com/ace-step/ACE-Step-1.5

---

## 🧩 Install notes
> ✅ **Python** and **Git** should be installed on your system  
> ⚡ Installer is created mostly for **CUDA / RTX**

1. 📦 Unzip to **C:\**
2. 🖱️ Double click **`install.bat`** (give one-time Windows permission to run unknown `.bat`)
3. 🧪 It will:
   - 🧰 Create the environment + install dependencies
   - ⬇️ Download repo + **Turbo model** + **1.7B LM**
4. ▶️ When finished, double click **`run.bat`** (give one-time permission)

⏳ **First-time use:** other models/LM will download on demand — expect slower results the first time.

---

## ✨ Features
- 🧠 Tooltips, **Auto**, **Let Ace decide**, and other helpful defaults for easy first-time use
- 🖱️🛡️ **Wheelguard** on/off (allow changes while scrolling on/off)
- 🏷️ **Fancy banner** on/off
- 🎨 **25 themes** (imported from my other app — not all look good / change much in this standalone version)
- 🎼 **Genre preset manager** (add/edit/delete)  
  - ✅ Comes with **60+ example presets**
  - 💾 Presets are saved with: **Thinking ON + SFT model + 1.7B LM**
  - ⚠️ This may require extra VRAM  
  - 📝 Tip: use Notepad++ to replace all models in the `.json` file at once with lower **Turbo + 0.6B** model/LM when needed  
  - 💾 Most things get saved (duration + a couple settings that change a lot don’t)
- 🎵 Mostly built around **Text → Music** (most main settings + toggles available)
  - 🧰 Other features like **cover**, **repaint**, etc. are available under **Advanced settings** (mostly untested)
- 🔍 Auto-checks for repo, environment, models, CLI, etc. at startup  
  - ♻️ If you already have models/LM's downloaded, you can copy them and re-use them
- 🕒 **Last results** (double click to play)
- 🏷️ Auto rename results (**genre - seed - date.mp3**)
- 🧊 Offload options (needs testing on -16GB VRAM cards)
- 🧾 Logger
- ✅ Online updater

---

## 🚫 Not working / not installed
- ⚡ Flash
- 🧪 nano-vllm (fallback to PyTorch)
- 🗣️ Tell me what else is not working.

---

## 🛠️ Todo
- 🚀 Toggle to keep the model in VRAM for faster generation
- 🔧 Try to fix Flash & nano-vllm when I find the time
- Add more functions /updates
