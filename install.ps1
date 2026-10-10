# Claude Blender Connector - Windows installer
# Downloads the latest release, installs + enables the Blender add-on, then opens the Claude extension installer.
$ErrorActionPreference = 'Stop'
$repo = 'RicheyWorks/claude-blender-connector'

if (Get-Process blender -ErrorAction SilentlyContinue) {
    Write-Host 'Blender is open. Save your work, close Blender, then run this again.' -ForegroundColor Yellow
    return
}

$candidates = @(
    (Get-ChildItem 'C:\Program Files\Blender Foundation\*\blender.exe' -ErrorAction SilentlyContinue),
    (Get-ChildItem "${env:ProgramFiles(x86)}\Steam\steamapps\common\Blender\blender.exe" -ErrorAction SilentlyContinue),
    (Get-Command blender.exe -ErrorAction SilentlyContinue | ForEach-Object { Get-Item $_.Source })
) | Where-Object { $_ } | Sort-Object FullName
if (-not $candidates) { Write-Host 'Could not find blender.exe. Install Blender 3.6 or newer first.' -ForegroundColor Red; return }
$blender = ($candidates | Select-Object -Last 1).FullName
Write-Host "Using $blender"

$dir = Join-Path $env:TEMP 'claude-blender-connector'
New-Item -ItemType Directory -Force -Path $dir | Out-Null
$base = "https://github.com/$repo/releases/latest/download"
$zip = Join-Path $dir 'claude_blender_bridge.zip'
$mcpb = Join-Path $dir 'blender-connector.mcpb'
Invoke-WebRequest "$base/claude_blender_bridge.zip" -OutFile $zip -UseBasicParsing
Invoke-WebRequest "$base/blender-connector.mcpb" -OutFile $mcpb -UseBasicParsing

$py = "import bpy; bpy.ops.preferences.addon_install(filepath=r'$zip', overwrite=True); bpy.ops.preferences.addon_enable(module='claude_blender_bridge'); bpy.ops.wm.save_userpref()"
& $blender -b --python-expr $py
Write-Host 'Blender add-on installed and enabled.' -ForegroundColor Green

Start-Process $blender
Start-Process $mcpb
Write-Host 'If Windows asks which app opens the .mcpb file, pick Claude, then click Install.' -ForegroundColor Cyan
