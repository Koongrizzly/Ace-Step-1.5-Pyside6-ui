# 🎛️ Ace-Step 1.5 Pyside6 ui
---
A different (Windows) look for Ace Step 1.5

| 🔎 Quick links | |
|---|---|
| 🧩 Install notes | [Jump](#-install-notes) |
| ✨ Features | [Jump](#-features-) |
| 🚫 Not working | [Jump](#-not-workingnot-installed) |
| 🛠️ Todo | [Jump](#-todo-) |

All credits to & original model here : https://github.com/ace-step/ACE-Step-1.5

## 🧩 Install notes :
(python and Git should be installed on your system, installer is created mostly for Cuda / RTX)
- unzip to C drive, double click 'install.bat', give one time permission (windows) to run unknown bat file.
- It will create environment and install dependencies, download repo and turbo model + 1.7B LM
- when finished double click 'run.bat' (give one time permission (windows) to run unknown bat file.)
- First time use of other models/LM will download them, expect slow results at first time use.
---
## ✨ Features :
- Tooltips, 'auto', 'let Ace decide' and other helpful defaults for easy first time use.
- Wheelguard on/off (allow changes while scrolling on/off)
- 'Fancy banner' on/off
- 25 themes (imported from my other app, not all look good or make a lot of change in this standalone version)
- Genre preset manager (add/edit/delete,..) comes with 60+ example presets, all presets are saved with thinking on + SFT model + 1.7B Lm, this may require extra vram, use an editor such as notepad++ to replace all models in the .json file at once with a lower (turbo and 0.6B) model/Lm when needed. Most things get saved , duration and a couple more that need change a lot don't.
Gow to add a preset ? It wll first ask for the main genre (eg 'Rock', next it asks for the subgenre (eg. 'metal') If you it already exists it will be overwritten (case sensitive)
- Mostly build around the text to music feature, all (most) main settings and toggles available.
  However all other features such as cover, repaint,.. are also available in the 'advanced settings' (but mostly untested)
- Auto checks for repo, environment and models, cli,... at startup, if you already have all models downloaded, you can simply copy them and re use them
- Last results (double click to play)
- Auto rename results (genre - seed - date . mp3)
- Offload options (needs testing on -16 gig vram cards)
- logger
- Online updater


## 🚫 Not working/not installed
- Flash
- nano-vllm (fallback to Pytorch)
- tell me what else is not working.

## 🛠️ Todo :
- toggle to keep the model in vram for faster generation
- try to fix flash & nano-vllm when i find the time
- Add more functions /updates
