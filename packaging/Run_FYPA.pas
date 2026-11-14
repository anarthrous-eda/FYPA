{..............................................................................
 Run_FYPA.pas

 Launch FYPA.py against the currently focused PCB project in Altium.

 Usage
   1. In Altium: DXP > Scripting System > Script Projects, add a new script
      project (or open an existing one) and add this .pas file to it.
   2. With a .PrjPcb open and focused in the Projects panel, right-click the
      `Run` procedure in the Script Editor and choose "Run Script".
   3. A console window opens and runs:
        <SCRIPT_DIR>\.venv\Scripts\python.exe FYPA.py gui <FocusedPrjPcb>

 To change which subcommand fires, edit SUBCOMMAND below
 (e.g. 'load', 'extract', 'annotations', 'geometry').
..............................................................................}

procedure Run;
const
    SCRIPT_DIR = 'C:\path\to\FYPA';
    SUBCOMMAND = 'gui';
var
    PyExe     : String;
    PyScript  : String;
    Workspace : IWorkspace;
    Project   : IProject;
    PrjPath   : String;
    Cmd       : String;
begin
    PyExe    := SCRIPT_DIR + '\.venv\Scripts\python.exe';
    PyScript := SCRIPT_DIR + '\FYPA.py';

    if not FileExists(PyExe) then
    begin
        ShowError('Python interpreter not found:'#13#10 + PyExe);
        Exit;
    end;
    if not FileExists(PyScript) then
    begin
        ShowError('FYPA.py not found:'#13#10 + PyScript);
        Exit;
    end;

    Workspace := GetWorkspace;
    if Workspace = nil then
    begin
        ShowError('No Altium workspace is open.');
        Exit;
    end;

    Project := Workspace.DM_FocusedProject;
    if Project = nil then
    begin
        ShowError('No project is currently focused. Open a PCB project first.');
        Exit;
    end;

    PrjPath := Project.DM_ProjectFullPath;
    if (PrjPath = '') or (not FileExists(PrjPath)) then
    begin
        ShowError('Focused project has not been saved to disk yet.');
        Exit;
    end;
    if LowerCase(ExtractFileExt(PrjPath)) <> '.prjpcb' then
    begin
        ShowError('Focused project is not a PCB project (.PrjPcb):'#13#10 + PrjPath);
        Exit;
    end;

    // Build full command:
    //   cmd.exe /K ""<python>" "<script>" <sub> "<prjpcb>""
    // The doubled outer quotes are how cmd.exe parses a /K command that
    // itself contains multiple quoted paths. /K keeps the console open after
    // Python exits so any tracebacks remain readable.
    Cmd := 'cmd.exe /K ""' + PyExe + '" "' + PyScript + '" ' +
           SUBCOMMAND + ' "' + PrjPath + '""';

    // RunApplication is Altium DelphiScript's built-in process launcher -
    // no `uses` clause needed, and unlike Windows API calls (ShellExecute,
    // WinExec) it's available in DelphiScript without external declarations.
    try
        RunApplication(Cmd);
    except
        ShowError('Failed to launch:'#13#10 + Cmd);
    end;
end;
