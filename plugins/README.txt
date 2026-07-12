DropGain local VST3 plugins (optional)
======================================

You do not need to use this folder. DropGain finds limiter plugins in the
normal system VST3 locations, or via PROL2_PLUGIN_PATH / LOUDMAX_PLUGIN_PATH.

This folder is only a convenience if you want a plugin sitting next to the app
instead of installing it system-wide. DropGain checks here before the standard
VST3 directories.

Optional: LoudMax
-----------------
LoudMax is distributed as a standalone .vst3 file with no installer. If you
prefer not to use the system VST3 folder, download LoudMax, copy the file
here, and select LoudMax in Preferences > Limiter engine.

FabFilter Pro-L 2
-----------------
Use the normal FabFilter installer and let DropGain auto-discover Pro-L 2 in
the system VST3 folder. Copying the .vst3 here only works if the plugin is
already installed to the default location first, so there is no practical
benefit to keeping a second copy in this folder.

Plugin binaries are ignored by Git. Do not commit commercial or proprietary
plugin files.

If you do add a plugin here, restart DropGain and use Preferences > Check
Limiter / System to verify it.
