"""
VDD Configuration & Quality Fix Helper
======================================
Sets Virtual Display Driver (VDD) to crisp native resolutions (2560x1440, 1920x1080)
and registers VDDPATH in Windows Registry, permanently preventing downscaling/blur issues.
Author: GeunHu Kim (delight0517)
License: MIT
"""

import os
import winreg

HIGH_RES_XML = """<?xml version='1.0' encoding='utf-8'?>
<vdd_settings>
    <monitors>
        <count>1</count>
    </monitors>
    <gpu>
        <friendlyname>default</friendlyname>
    </gpu>
    <global>
        <g_refresh_rate>60</g_refresh_rate>
        <g_refresh_rate>120</g_refresh_rate>
        <g_refresh_rate>144</g_refresh_rate>
    </global>
    <resolutions>
        <resolution>
            <width>2560</width>
            <height>1440</height>
            <refresh_rate>60</refresh_rate>
        </resolution>
        <resolution>
            <width>1920</width>
            <height>1080</height>
            <refresh_rate>60</refresh_rate>
        </resolution>
    </resolutions>
    <options>
        <CustomEdid>false</CustomEdid>
        <PreventSpoof>false</PreventSpoof>
        <EdidCeaOverride>false</EdidCeaOverride>
        <HardwareCursor>true</HardwareCursor>
        <SDR10bit>false</SDR10bit>
        <HDRPlus>false</HDRPlus>
        <logging>false</logging>
        <debuglogging>false</debuglogging>
    </options>
</vdd_settings>
"""

def setup_high_res_config(target_dir: str):
    os.makedirs(target_dir, exist_ok=True)
    xml_path = os.path.join(target_dir, "vdd_settings.xml")
    with open(xml_path, "w", encoding="utf-8") as f:
        f.write(HIGH_RES_XML)
    print(f"✅ Generated high-res config: {xml_path}")
    
    # Register in HKCU
    key = winreg.CreateKey(winreg.HKEY_CURRENT_USER, r"SOFTWARE\MikeTheTech\VirtualDisplayDriver")
    winreg.SetValueEx(key, "VDDPATH", 0, winreg.REG_SZ, target_dir)
    print(f"✅ Registered VDDPATH in HKCU: {target_dir}")

if __name__ == "__main__":
    default_dir = os.path.join(os.environ.get("LOCALAPPDATA", "C:\\"), "VirtualDisplayDriver")
    setup_high_res_config(default_dir)
