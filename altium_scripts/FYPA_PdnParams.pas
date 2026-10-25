{ FYPA_PdnParams.pas  (+ FYPA_PdnParams.dfm)
  Add / edit the FYPA PDN_* parameters of one schematic component.

  Usage
    1. Add FYPA_PdnParams.pas to a script project (the .dfm beside it is
       picked up automatically), or open FYPA_Scripts.PrjScr.
    2. Open a .SchDoc and select ONE component (selecting several parts of
       the same multi-part component is also fine).
    3. Run FYPA_PdnParams.  The dialog shows:
         - Channel    part-wide PDN_* or an indexed channel PDN<n>_*
         - Role       SOURCE / SINK / SERIES / REGULATOR / PATH, or
                      "(none)" to strip that channel's parameters
         - Terminals  single-net (PDN_NET) or two-terminal (P / N), for
                      SOURCE and SINK only
         - the fields that role accepts.  Net boxes list the nets on the
           component's own pins (typing any other net name is allowed).
       The right-hand side lists the component's pins with their nets and
       every PDN parameter already on the part.
    4. Apply writes the channel, OK writes and closes.  Each Apply is one
       undo step.

  What Apply does to the selected channel
    - writes PDN_ROLE (or PDN<n>_ROLE) plus every non-blank field
    - updates existing parameters in place, adds missing ones
    - REMOVES every other parameter of that channel (blank fields, keys
      left over from a previous role or terminal mode, unknown keys).
      Parameters of other channels are never touched.
    - applies to every part with the same designator on this sheet.
      Parts of the same component placed on other sheets are NOT updated.

  Checks before writing (mirrors fypa/altium/annotations.py)
    Errors block the write:  missing required value, a value FYPA cannot
      parse (forms like 100mA, 3V3, 0R01, 1.5E-9), R <= 0, efficiency
      outside (0, 1], quiescent < 0, missing terminals, P net = N net,
      REGULATOR without REGULATOR_TYPE or GAIN.
    Warnings ask first:  a net that is on none of this part's pins, a pin
      the part does not have, GAIN together with REGULATOR_TYPE, and so on.

  Pin nets come from the focused project's LAST compile - the script does
  not compile on start-up because that freezes Altium on large projects.
  If you have changed wiring since, press "Compile project & refresh nets".
  If the sheet is not in the focused project, or there is no compile data
  for this part, the pin list falls
  back to the symbol pins with no nets; net boxes are then empty but still
  accept typed names, and the "net not on this part" warning is skipped.
}

Var
    gSch        : ISch_Document;
    gComp       : ISch_Component;  { the selected part; parameters are read from it }
    gDesignator : String;
    gPinNums    : TStringList;     { unique pin numbers of the component        }
    gNets       : TStringList;     { sorted unique nets on the component's pins }
    gNetsKnown  : Boolean;         { False when the compile gave no pin nets     }
    gChanIdx    : TStringList;     { parallel to cbChannel.Items, '' = part-wide }
    gFieldKeys  : TStringList;     { suffix per field row, e.g. 'P_NET'          }
    gFieldKinds : TStringList;     { VALUE / NET / PINS / DES / ENUM             }
    gFieldCtrls : TList;           { input control per field row                 }
    gDynCtrls   : TList;           { every runtime control, freed on rebuild     }
    gValCache   : TStringList;     { SUFFIX=value, survives role / mode switches }
    gRowY       : Integer;
    gLoading    : Boolean;
    gSIValue    : Double;          { output of ParseSI                           }
    gAdded, gChanged, gRemoved, gParts : Integer;


{ =======================================================================
  Small string helpers
  ======================================================================= }
Function IsDigitCh(C : String) : Boolean;
Begin
    Result := (C <> '') And (C >= '0') And (C <= '9');
End;

{ ASCII letters, '%', and anything non-ASCII (micro sign, ohm sign) }
Function IsLetterCh(C : String) : Boolean;
Begin
    Result := (C <> '') And (((C >= 'a') And (C <= 'z')) Or
                             ((C >= 'A') And (C <= 'Z')) Or
                             (C = '%') Or (C > '~'));
End;

{ Split a pin list on commas and whitespace, like FYPA's _split_pin_list }
Procedure SplitPins(S : String; Dest : TStringList);
Var
    I   : Integer;
    C   : String;
    Tok : String;
Begin
    Dest.Clear;
    Tok := '';
    For I := 1 To Length(S) Do
    Begin
        C := Copy(S, I, 1);
        If (C = ',') Or (C = ' ') Or (C = #9) Or (C = ';') Then
        Begin
            If Tok <> '' Then Dest.Add(Tok);
            Tok := '';
        End
        Else
            Tok := Tok + C;
    End;
    If Tok <> '' Then Dest.Add(Tok);
End;


{ =======================================================================
  SI value parser - a port of fypa/altium/annotations.py parse_si_value.
  Returns True and sets gSIValue when FYPA would accept the text.
  ======================================================================= }
Function PrefixMult(C : String) : Double;   { 0 when C is not an SI prefix }
Begin
    Result := 0;
    If C = 'f' Then Result := 1E-15
    Else If C = 'p' Then Result := 1E-12
    Else If C = 'n' Then Result := 1E-9
    Else If C = 'u' Then Result := 1E-6
    Else If C = 'm' Then Result := 1E-3
    Else If (C = 'k') Or (C = 'K') Then Result := 1E3
    Else If C = 'M' Then Result := 1E6
    Else If C = 'G' Then Result := 1E9
    Else If C = 'T' Then Result := 1E12
    Else If (C <> '') And (C > '~') Then Result := 1E-6;   { micro sign }
End;

Function IsUnitWord(S : String) : Boolean;
Var
    L : String;
Begin
    L := LowerCase(S);
    Result := (L = 'v') Or (L = 'a') Or (L = 'ohm') Or (L = 'r') Or
              (L = 'hz') Or (L = 's') Or (L = 'f') Or (L = 'h') Or
              (L = '%') Or ((Length(S) = 1) And (S > '~'));   { ohm sign }
End;

Function DigitsValue(IntStr : String; FracStr : String) : Double;
Var
    I : Integer;
    D : Double;
Begin
    Result := 0;
    For I := 1 To Length(IntStr) Do
        Result := Result * 10 + StrToInt(Copy(IntStr, I, 1));
    D := 1;
    For I := 1 To Length(FracStr) Do
    Begin
        D := D / 10;
        Result := Result + StrToInt(Copy(FracStr, I, 1)) * D;
    End;
End;

Function ParseSI(S : String) : Boolean;
Var
    T, C, IntStr, FracStr, ExpStr, Rest, Letter : String;
    P, I, K, ExpSign : Integer;
    Sign, Mag, M : Double;
Begin
    Result   := False;
    gSIValue := 0;

    { drop spaces, e.g. '100 mA' -> '100mA' }
    T := '';
    For I := 1 To Length(S) Do
        If (Copy(S, I, 1) <> ' ') And (Copy(S, I, 1) <> #9) Then
            T := T + Copy(S, I, 1);
    If T = '' Then Exit;

    P    := 1;
    Sign := 1;
    C    := Copy(T, P, 1);
    If (C = '+') Or (C = '-') Then
    Begin
        If C = '-' Then Sign := -1;
        Inc(P);
    End;

    IntStr := '';
    While IsDigitCh(Copy(T, P, 1)) Do
    Begin
        IntStr := IntStr + Copy(T, P, 1);
        Inc(P);
    End;

    FracStr := '';
    If IntStr = '' Then
    Begin
        { leading-dot form: .001 }
        If Copy(T, P, 1) <> '.' Then Exit;
        Inc(P);
        While IsDigitCh(Copy(T, P, 1)) Do
        Begin
            FracStr := FracStr + Copy(T, P, 1);
            Inc(P);
        End;
        If FracStr = '' Then Exit;
    End
    Else If Copy(T, P, 1) = '.' Then
    Begin
        Inc(P);
        While IsDigitCh(Copy(T, P, 1)) Do
        Begin
            FracStr := FracStr + Copy(T, P, 1);
            Inc(P);
        End;
    End
    Else
    Begin
        { engineering form: 3V3, 4k7, 0R01, 500mA, 7m.  A letter right after
          the integer, unless it is the 'e' of scientific notation. }
        C := Copy(T, P, 1);
        If IsLetterCh(C) And
           Not (((C = 'e') Or (C = 'E')) And
                (IsDigitCh(Copy(T, P + 1, 1)) Or
                 (((Copy(T, P + 1, 1) = '+') Or (Copy(T, P + 1, 1) = '-')) And
                  IsDigitCh(Copy(T, P + 2, 1))))) Then
        Begin
            Letter := C;
            Inc(P);
            While IsDigitCh(Copy(T, P, 1)) Do
            Begin
                FracStr := FracStr + Copy(T, P, 1);
                Inc(P);
            End;
            { the rest must be letters; FYPA ignores it in this form }
            For I := P To Length(T) Do
                If Not IsLetterCh(Copy(T, I, 1)) Then Exit;
            Mag := DigitsValue(IntStr, FracStr);
            M   := PrefixMult(Letter);
            If M <> 0 Then Mag := Mag * M;
            gSIValue := Sign * Mag;
            Result   := True;
            Exit;
        End;
    End;

    Mag := DigitsValue(IntStr, FracStr);

    { scientific exponent }
    C := Copy(T, P, 1);
    If (C = 'e') Or (C = 'E') Then
    Begin
        ExpSign := 1;
        I := P + 1;
        If (Copy(T, I, 1) = '+') Or (Copy(T, I, 1) = '-') Then
        Begin
            If Copy(T, I, 1) = '-' Then ExpSign := -1;
            Inc(I);
        End;
        If IsDigitCh(Copy(T, I, 1)) Then
        Begin
            ExpStr := '';
            While IsDigitCh(Copy(T, I, 1)) Do
            Begin
                ExpStr := ExpStr + Copy(T, I, 1);
                Inc(I);
            End;
            For K := 1 To StrToInt(ExpStr) Do
                If ExpSign > 0 Then Mag := Mag * 10 Else Mag := Mag / 10;
            P := I;
        End;
    End;

    Rest := Copy(T, P, Length(T));
    For I := 1 To Length(Rest) Do
        If Not IsLetterCh(Copy(Rest, I, 1)) Then Exit;

    { suffix: a bare unit, or an SI prefix optionally followed by a unit;
      anything else is ignored, exactly as FYPA does }
    If (Rest <> '') And Not IsUnitWord(Rest) Then
    Begin
        M := PrefixMult(Copy(Rest, 1, 1));
        If (M <> 0) And ((Length(Rest) = 1) Or IsUnitWord(Copy(Rest, 2, Length(Rest)))) Then
            Mag := Mag * M;
    End;

    gSIValue := Sign * Mag;
    Result   := True;
End;


{ =======================================================================
  Roles, channels and parameter-name helpers
  ======================================================================= }
Function RoleAt(I : Integer) : String;
Begin
    If I = 0 Then Result := 'SOURCE'
    Else If I = 1 Then Result := 'SINK'
    Else If I = 2 Then Result := 'SERIES'
    Else If I = 3 Then Result := 'REGULATOR'
    Else If I = 4 Then Result := 'PATH'
    Else Result := 'NONE';
End;

Function RoleIndex(Role : String) : Integer;
Var
    I : Integer;
Begin
    Result := -1;
    For I := 0 To 4 Do
        If RoleAt(I) = UpperCase(Trim(Role)) Then Result := I;
End;

Function CurrentRole(Dummy : Integer) : String;
Begin
    Result := RoleAt(FYPAParamForm.cbRole.ItemIndex);
End;

Function CurrentChanIdx(Dummy : Integer) : String;
Begin
    Result := '';
    If FYPAParamForm.cbChannel.ItemIndex >= 0 Then
        Result := gChanIdx[FYPAParamForm.cbChannel.ItemIndex];
End;

Function ChanPrefix(Idx : String) : String;
Begin
    If Idx = '' Then Result := 'PDN_' Else Result := 'PDN' + Idx + '_';
End;

{ Channel index of a PDN parameter name: '' for PDN_X, '3' for PDN3_X,
  '-' when the name is not a PDN key (e.g. the pin-level 'PDN'). }
Function ParamChannel(PName : String) : String;
Var
    U, Digits : String;
    P : Integer;
Begin
    Result := '-';
    U := UpperCase(Trim(PName));
    If Copy(U, 1, 3) <> 'PDN' Then Exit;
    P := 4;
    Digits := '';
    While IsDigitCh(Copy(U, P, 1)) Do
    Begin
        Digits := Digits + Copy(U, P, 1);
        Inc(P);
    End;
    If Copy(U, P, 1) <> '_' Then Exit;
    If Length(U) <= P Then Exit;
    If Digits = '' Then Result := ''
    Else Result := IntToStr(StrToInt(Digits));   { PDN01_ is channel 1 }
End;

Function ParamSuffix(PName : String) : String;
Var
    U : String;
Begin
    U := UpperCase(Trim(PName));
    Result := Copy(U, Pos('_', U) + 1, Length(U));
End;

{ Suffix of PName when it belongs to channel Idx, else '' }
Function ChannelSuffix(PName : String; Idx : String) : String;
Begin
    Result := '';
    If ParamChannel(PName) = Idx Then Result := ParamSuffix(PName);
End;


{ =======================================================================
  Pins, nets and existing parameters
  ======================================================================= }
Procedure AddPinRow(PinNum : String; PinName : String; NetName : String);
Var
    Row : String;
Begin
    If (PinNum = '') Or (gPinNums.IndexOf(PinNum) >= 0) Then Exit;
    gPinNums.Add(PinNum);
    If (NetName <> '') And (NetName <> '?') And (gNets.IndexOf(NetName) < 0) Then
        gNets.Add(NetName);
    If (NetName = '') Or (NetName = '?') Then NetName := '(no net)';
    Row := PinNum;
    While Length(Row) < 6 Do Row := Row + ' ';
    Row := Row + ' ' + PinName;
    While Length(Row) < 22 Do Row := Row + ' ';
    FYPAParamForm.lbPins.Items.Add(Row + ' ' + NetName);
End;

{ DoCompile = False reads whatever the last compile left behind - compiling
  a large project takes long enough to freeze Altium, so it only happens
  when the user presses the Compile button. }
Procedure GatherPinsAndNets(DoCompile : Boolean);
Var
    Workspace : IWorkspace;
    Project   : IProject;
    PhysDoc   : IDocument;
    TempComp  : IComponent;
    TempPart  : IPart;
    TempPin   : IPin;
    CompIter  : ISch_Iterator;
    PinIter   : ISch_Iterator;
    SchComp   : ISch_Component;
    SchPin    : ISch_Pin;
    D, J, K, I2 : Integer;
    UDes      : String;
Begin
    gPinNums.Clear;
    gNets.Clear;
    FYPAParamForm.lbPins.Items.Clear;
    UDes := UpperCase(gDesignator);

    { --- compiled project: pin numbers, names and flattened nets --- }
    Workspace := GetWorkspace;
    Project   := Nil;
    If Workspace <> Nil Then Project := Workspace.DM_FocusedProject;
    If Project <> Nil Then
    Begin
        If DoCompile Then Project.DM_Compile;
        For D := 0 To Project.DM_PhysicalDocumentCount - 1 Do
        Begin
            PhysDoc := Project.DM_PhysicalDocuments(D);
            If UpperCase(PhysDoc.DM_DocumentKind) <> 'SCH' Then Continue;
            For J := 0 To PhysDoc.DM_ComponentCount - 1 Do
            Begin
                TempComp := PhysDoc.DM_Components(J);
                If (UpperCase(TempComp.DM_LogicalDesignator) <> UDes) And
                   (UpperCase(TempComp.DM_PhysicalDesignator) <> UDes) Then Continue;
                If TempComp.DM_SubPartCount <= 1 Then
                Begin
                    For K := 0 To TempComp.DM_PinCount - 1 Do
                    Begin
                        TempPin := TempComp.DM_Pins(K);
                        AddPinRow(TempPin.DM_PinNumber, TempPin.DM_PinName,
                                  TempPin.DM_FlattenedNetName);
                    End;
                End
                Else
                Begin
                    For I2 := 0 To TempComp.DM_SubPartCount - 1 Do
                    Begin
                        TempPart := TempComp.DM_SubParts(I2);
                        For K := 0 To TempPart.DM_PinCount - 1 Do
                        Begin
                            TempPin := TempPart.DM_Pins(K);
                            AddPinRow(TempPin.DM_PinNumber, TempPin.DM_PinName,
                                      TempPin.DM_FlattenedNetName);
                        End;
                    End;
                End;
            End;
        End;
    End;
    gNetsKnown := gNets.Count > 0;

    { --- fallback / top-up: symbol pins on this sheet, no nets --- }
    CompIter := gSch.SchIterator_Create;
    CompIter.AddFilter_ObjectSet(MkSet(eSchComponent));
    Try
        SchComp := CompIter.FirstSchObject;
        While SchComp <> Nil Do
        Begin
            If UpperCase(SchComp.Designator.Text) = UDes Then
            Begin
                PinIter := SchComp.SchIterator_Create;
                PinIter.AddFilter_ObjectSet(MkSet(ePin));
                Try
                    SchPin := PinIter.FirstSchObject;
                    While SchPin <> Nil Do
                    Begin
                        AddPinRow(Trim(SchPin.Designator), SchPin.Name, '');
                        SchPin := PinIter.NextSchObject;
                    End;
                Finally
                    SchComp.SchIterator_Destroy(PinIter);
                End;
            End;
            SchComp := CompIter.NextSchObject;
        End;
    Finally
        gSch.SchIterator_Destroy(CompIter);
    End;

    If Not gNetsKnown Then
    Begin
        If DoCompile Then
            FYPAParamForm.gbPins.Caption := 'Component pins   (no nets - compile found none)'
        Else
            FYPAParamForm.gbPins.Caption := 'Component pins   (no nets - press Compile)';
    End
    Else
        FYPAParamForm.gbPins.Caption := 'Component pins   (pin / name / net)';
End;

Procedure RefreshExisting(Dummy : Integer);
Var
    Iter  : ISch_Iterator;
    Param : ISch_Parameter;
    Memo  : TMemo;
Begin
    Memo := FYPAParamForm.memoExisting;
    Memo.Lines.Clear;
    Iter := gComp.SchIterator_Create;
    Iter.AddFilter_ObjectSet(MkSet(eParameter));
    Try
        Param := Iter.FirstSchObject;
        While Param <> Nil Do
        Begin
            If ParamChannel(Param.Name) <> '-' Then
                Memo.Lines.Add(Param.Name + ' = ' + Param.Text);
            Param := Iter.NextSchObject;
        End;
    Finally
        gComp.SchIterator_Destroy(Iter);
    End;
    If Memo.Lines.Count = 0 Then Memo.Lines.Add('(none)');
End;

{ Rebuild the channel list from the parameters on the part and select
  channel SelectIdx when it still exists (otherwise part-wide). }
Procedure FillChannels(SelectIdx : String);
Var
    Iter   : ISch_Iterator;
    Param  : ISch_Parameter;
    Found  : TStringList;
    Ch, Padded : String;
    I, MaxIdx  : Integer;
    Cb     : TComboBox;
Begin
    Cb := FYPAParamForm.cbChannel;
    Found := TStringList.Create;
    Try
        Found.Sorted := True;
        Iter := gComp.SchIterator_Create;
        Iter.AddFilter_ObjectSet(MkSet(eParameter));
        Try
            Param := Iter.FirstSchObject;
            While Param <> Nil Do
            Begin
                Ch := ParamChannel(Param.Name);
                If (Ch <> '-') And (Ch <> '') Then
                Begin
                    Padded := Ch;
                    While Length(Padded) < 6 Do Padded := '0' + Padded;
                    If Found.IndexOf(Padded) < 0 Then Found.Add(Padded);
                End;
                Param := Iter.NextSchObject;
            End;
        Finally
            gComp.SchIterator_Destroy(Iter);
        End;

        Cb.Items.Clear;
        gChanIdx.Clear;
        Cb.Items.Add('Part-wide          (PDN_ROLE, PDN_V, ...)');
        gChanIdx.Add('');
        MaxIdx := 0;
        For I := 0 To Found.Count - 1 Do
        Begin
            Ch := IntToStr(StrToInt(Found[I]));
            Cb.Items.Add('Channel ' + Ch + '          (PDN' + Ch + '_*)');
            gChanIdx.Add(Ch);
            MaxIdx := StrToInt(Ch);
        End;
        Ch := IntToStr(MaxIdx + 1);
        Cb.Items.Add('New channel ' + Ch + '      (PDN' + Ch + '_*)');
        gChanIdx.Add(Ch);
    Finally
        Found.Free;
    End;

    I := gChanIdx.IndexOf(SelectIdx);
    If I < 0 Then I := 0;
    Cb.ItemIndex := I;
End;


{ =======================================================================
  Dynamic field rows
  ======================================================================= }
Procedure SaveFieldsToCache(Dummy : Integer);
Var
    I : Integer;
Begin
    For I := 0 To gFieldKeys.Count - 1 Do
        gValCache.Values[gFieldKeys[I]] := Trim(gFieldCtrls.Items[I].Text);
End;

Procedure ClearFields(Dummy : Integer);
Var
    I : Integer;
Begin
    For I := gDynCtrls.Count - 1 DownTo 0 Do
        gDynCtrls.Items[I].Free;
    gDynCtrls.Clear;
    gFieldCtrls.Clear;
    gFieldKeys.Clear;
    gFieldKinds.Clear;
End;

Procedure AddHeader(Caption : String);
Var
    Lbl : TLabel;
Begin
    Lbl := TLabel.Create(FYPAParamForm.sbFields);
    Lbl.Parent     := FYPAParamForm.sbFields;
    Lbl.Left       := 6;
    Lbl.Top        := gRowY + 4;
    Lbl.Caption    := Caption;
    Lbl.Font.Style := MkSet(fsBold);
    gDynCtrls.Add(Lbl);
    gRowY := gRowY + 24;
End;

{ Kind: VALUE / NET / PINS / DES / ENUM.  Options (ENUM only) is a comma
  list whose first entry may be empty. }
Procedure AddField(Key : String; Caption : String; Kind : String;
                   Required : Boolean; Hint : String; Options : String);
Var
    Lbl, HintLbl : TLabel;
    Edt  : TEdit;
    Cmb  : TComboBox;
    Ctrl : TObject;
    Val, Val2 : String;
    I    : Integer;
Begin
    Lbl := TLabel.Create(FYPAParamForm.sbFields);
    Lbl.Parent  := FYPAParamForm.sbFields;
    Lbl.Left    := 14;
    Lbl.Top     := gRowY + 3;
    If Required Then Lbl.Caption := Caption + ' *' Else Lbl.Caption := Caption;
    gDynCtrls.Add(Lbl);

    Val := gValCache.Values[Key];

    If (Kind = 'NET') Or (Kind = 'ENUM') Then
    Begin
        Cmb := TComboBox.Create(FYPAParamForm.sbFields);
        Cmb.Parent := FYPAParamForm.sbFields;
        Cmb.Left   := 150;
        Cmb.Top    := gRowY;
        Cmb.Width  := 160;
        If Kind = 'NET' Then
        Begin
            Cmb.Style := csDropDown;
            For I := 0 To gNets.Count - 1 Do Cmb.Items.Add(gNets[I]);
            Cmb.Text := Val;
        End
        Else
        Begin
            Cmb.Style := csDropDownList;
            { split on commas, keeping empty entries (',LDO,SMPS' -> '', LDO, SMPS) }
            Val2 := Options;
            While Val2 <> '' Do
            Begin
                I := Pos(',', Val2);
                If I = 0 Then
                Begin
                    Cmb.Items.Add(Val2);
                    Val2 := '';
                End
                Else
                Begin
                    Cmb.Items.Add(Copy(Val2, 1, I - 1));
                    Val2 := Copy(Val2, I + 1, Length(Val2));
                    If Val2 = '' Then Cmb.Items.Add('');
                End;
            End;
            { keep an unrecognised existing value rather than losing it }
            If (Val <> '') And (Cmb.Items.IndexOf(UpperCase(Val)) < 0) Then
                Cmb.Items.Add(Val);
            I := Cmb.Items.IndexOf(UpperCase(Val));
            If I < 0 Then I := Cmb.Items.IndexOf(Val);
            If I < 0 Then I := 0;
            Cmb.ItemIndex := I;
        End;
        Ctrl := Cmb;
    End
    Else
    Begin
        Edt := TEdit.Create(FYPAParamForm.sbFields);
        Edt.Parent := FYPAParamForm.sbFields;
        Edt.Left   := 150;
        Edt.Top    := gRowY;
        Edt.Width  := 160;
        Edt.Text   := Val;
        Ctrl := Edt;
    End;
    gDynCtrls.Add(Ctrl);

    HintLbl := TLabel.Create(FYPAParamForm.sbFields);
    HintLbl.Parent     := FYPAParamForm.sbFields;
    HintLbl.Left       := 316;
    HintLbl.Top        := gRowY + 3;
    HintLbl.Caption    := Hint;
    HintLbl.Font.Color := clGray;
    gDynCtrls.Add(HintLbl);

    gFieldKeys.Add(Key);
    gFieldKinds.Add(Kind);
    gFieldCtrls.Add(Ctrl);
    gRowY := gRowY + 26;
End;

Function RoleHelp(Role : String) : String;
Begin
    If Role = 'SOURCE' Then
        Result := 'Voltage source.  PDN_V is required.  Single-net mode ties the N side ' +
                  'to an ideal 0 ohm return; two-terminal mode drives P relative to N.'
    Else If Role = 'SINK' Then
        Result := 'Current load.  PDN_I is required.  MIN_V adds a pass/fail check, ' +
                  'PIN_R replaces the default 100 mohm pin coupling resistance.'
    Else If Role = 'SERIES' Then
        Result := 'Resistive element between two nets (shunt, ferrite, fuse, FET ...).  ' +
                  'PDN_R > 0 is required.  Nets may be left blank on a single-channel ' +
                  'two-pad part - FYPA infers them.  P = upstream (supply side), ' +
                  'N = downstream: regulators find Vin by walking N -> P.'
    Else If Role = 'REGULATOR' Then
        Result := 'Regulator.  PDN_V plus output and input P/N terminals are required.  ' +
                  'Set REGULATOR_TYPE (LDO or SMPS), or an explicit GAIN which overrides it.'
    Else If Role = 'PATH' Then
        Result := 'Switch path of an external-FET SMPS.  PDN_R is required; SMPS_HOST ' +
                  'names the controller (e.g. U3).'
    Else
        Result := 'Apply removes every parameter of this channel from the part.';
End;

Procedure AddTerminalsSourceSink(Single : Boolean);
Begin
    AddHeader('Terminals');
    If Single Then
    Begin
        AddField('NET',  'Net',  'NET',  False, 'net the part connects to', '');
        AddField('PINS', 'Pins', 'PINS', False, 'overrides Net, e.g. 1,2', '');
    End
    Else
    Begin
        AddField('P_NET',  'P net (+)',  'NET',  False, 'positive side net', '');
        AddField('N_NET',  'N net (-)',  'NET',  False, 'return side net', '');
        AddField('P_PINS', 'P pins',     'PINS', False, 'overrides P net', '');
        AddField('N_PINS', 'N pins',     'PINS', False, 'overrides N net', '');
        AddField('P_DES',  'P from parts', 'DES', False, 'other parts, e.g. C1,C2', '');
        AddField('N_DES',  'N from parts', 'DES', False, 'other parts, e.g. C1,C2', '');
    End;
End;

Procedure AddTerminalsTwoNet(Dummy : Integer);
Begin
    AddHeader('Terminals');
    AddField('P_NET',  'P net',  'NET',  False, 'upstream (supply) net', '');
    AddField('N_NET',  'N net',  'NET',  False, 'downstream (load) net', '');
    AddField('P_PINS', 'P pins', 'PINS', False, 'overrides P net', '');
    AddField('N_PINS', 'N pins', 'PINS', False, 'overrides N net', '');
End;

Procedure BuildFields(KeepValues : Boolean);
Var
    Role, Idx : String;
    Single    : Boolean;
    Help      : String;
Begin
    If KeepValues Then SaveFieldsToCache(0);
    ClearFields(0);
    FYPAParamForm.sbFields.VertScrollBar.Position := 0;
    gRowY := 4;

    Role   := CurrentRole(0);
    Idx    := CurrentChanIdx(0);
    Single := FYPAParamForm.cbMode.ItemIndex = 0;
    FYPAParamForm.cbMode.Enabled := (Role = 'SOURCE') Or (Role = 'SINK');

    Help := RoleHelp(Role);
    If (Idx <> '') And (Role <> 'NONE') Then
        Help := Help + '  Blank fields inherit PDN_<key> when this channel''s role matches PDN_ROLE.';
    FYPAParamForm.lblHelp.Caption := Help;

    If Role = 'SOURCE' Then
    Begin
        AddHeader('Value');
        AddField('V', 'Voltage', 'VALUE', True, 'e.g. 3V3, 5V, 1.8', '');
        AddTerminalsSourceSink(Single);
    End
    Else If Role = 'SINK' Then
    Begin
        AddHeader('Value');
        AddField('I',     'Current',        'VALUE', True,  'e.g. 500mA, 1.2A', '');
        AddField('MIN_V', 'Min voltage',    'VALUE', False, 'optional pass/fail', '');
        AddField('PIN_R', 'Pin resistance', 'VALUE', False, 'default 100m (ohm)', '');
        AddTerminalsSourceSink(Single);
    End
    Else If Role = 'SERIES' Then
    Begin
        AddHeader('Value');
        AddField('R', 'Resistance', 'VALUE', True, 'e.g. 10m, 0R01, 5mR', '');
        AddTerminalsTwoNet(0);
    End
    Else If Role = 'PATH' Then
    Begin
        AddHeader('Value');
        AddField('R',         'Resistance', 'VALUE', True,  'e.g. 10m (Rds_on)', '');
        AddField('SMPS_HOST', 'SMPS host',  'DES',   False, 'controller, e.g. U3', '');
        AddTerminalsTwoNet(0);
    End
    Else If Role = 'REGULATOR' Then
    Begin
        AddHeader('Output');
        AddField('V',          'Output voltage', 'VALUE', True,  'e.g. 1V8', '');
        AddField('OUT_P_NET',  'Out + net',  'NET',  False, 'or Out + pins', '');
        AddField('OUT_N_NET',  'Out - net',  'NET',  False, 'or Out - pins', '');
        AddField('OUT_P_PINS', 'Out + pins', 'PINS', False, 'overrides Out + net', '');
        AddField('OUT_N_PINS', 'Out - pins', 'PINS', False, 'overrides Out - net', '');
        AddHeader('Input');
        AddField('IN_P_NET',   'In + net',   'NET',  False, 'or In + pins', '');
        AddField('IN_N_NET',   'In - net',   'NET',  False, 'or In - pins', '');
        AddField('IN_P_PINS',  'In + pins',  'PINS', False, 'overrides In + net', '');
        AddField('IN_N_PINS',  'In - pins',  'PINS', False, 'overrides In - net', '');
        AddHeader('Gain');
        AddField('REGULATOR_TYPE',       'Regulator type', 'ENUM',  False, 'LDO: gain 1', ',LDO,SMPS');
        AddField('REGULATOR_EFFICIENCY', 'Efficiency',     'VALUE', False, 'SMPS, 0..1 (def 1)', '');
        AddField('GAIN',                 'Gain',           'VALUE', False, 'overrides type', '');
        AddField('QUIESCENT',            'Quiescent current', 'VALUE', False, 'default 0', '');
        AddHeader('SMPS (optional)');
        AddField('SMPS_TOPOLOGY', 'Topology', 'ENUM', False, 'SMPS only',
                 ',BUCK,BOOST,BUCKBOOST,INVERTER');
        AddField('SW1_NET', 'Switch node 1', 'NET', False, 'SW net', '');
        AddField('SW2_NET', 'Switch node 2', 'NET', False, 'buck-boost 2nd SW', '');
    End;

    If Role <> 'NONE' Then
    Begin
        AddHeader('Pin filters');
        AddField('IGNORE_PINS', 'Ignore pins', 'PINS', False, 'never used, e.g. 4,EP', '');
        If Idx = '' Then
        Begin
            AddField('PINS_ONLY',  'Pins only',  'PINS', False, 'allowlist, part-wide', '');
            AddField('EXTRA_PINS', 'Extra pins', 'PINS', False, 'added to allowlist', '');
        End;
    End;
End;

{ Load the selected channel's parameters into the cache and show them }
Procedure LoadChannel(Dummy : Integer);
Var
    Iter  : ISch_Iterator;
    Param : ISch_Parameter;
    Idx, Sfx, Role, PartRole, Des : String;
    Single : Boolean;
Begin
    Idx := CurrentChanIdx(0);
    gValCache.Clear;
    PartRole := '';
    Iter := gComp.SchIterator_Create;
    Iter.AddFilter_ObjectSet(MkSet(eParameter));
    Try
        Param := Iter.FirstSchObject;
        While Param <> Nil Do
        Begin
            Sfx := ChannelSuffix(Param.Name, Idx);
            If (Sfx <> '') And (gValCache.IndexOfName(Sfx) < 0) Then
                gValCache.Values[Sfx] := Trim(Param.Text);
            If ChannelSuffix(Param.Name, '') = 'ROLE' Then
                PartRole := UpperCase(Trim(Param.Text));
            Param := Iter.NextSchObject;
        End;
    Finally
        gComp.SchIterator_Destroy(Iter);
    End;

    Role := gValCache.Values['ROLE'];
    If Role = '' Then
    Begin
        { new channel: follow the part-wide role, else guess from the designator }
        Role := PartRole;
        If Role = '' Then
        Begin
            Des := UpperCase(gDesignator);
            If (Copy(Des, 1, 1) = 'R') Or (Copy(Des, 1, 1) = 'L') Or
               (Copy(Des, 1, 1) = 'F') Then Role := 'SERIES'
            Else Role := 'SINK';
        End;
    End;
    If RoleIndex(Role) < 0 Then Role := 'SINK';

    Single := (gValCache.Values['NET'] <> '') Or (gValCache.Values['PINS'] <> '');

    gLoading := True;
    Try
        FYPAParamForm.cbRole.ItemIndex := RoleIndex(Role);
        If Single Then FYPAParamForm.cbMode.ItemIndex := 0
        Else FYPAParamForm.cbMode.ItemIndex := 1;
    Finally
        gLoading := False;
    End;
    BuildFields(False);
End;


{ =======================================================================
  Validation
  ======================================================================= }
Function FieldVal(Key : String) : String;
Var
    I : Integer;
Begin
    Result := '';
    I := gFieldKeys.IndexOf(Key);
    If I >= 0 Then Result := Trim(gFieldCtrls.Items[I].Text);
End;

Function HasField(Key : String) : Boolean;
Begin
    Result := gFieldKeys.IndexOf(Key) >= 0;
End;

{ True when the terminal has a net or pins, e.g. Side='OUT_P' or 'P' }
Function SideSet(Side : String) : Boolean;
Begin
    Result := (FieldVal(Side + '_NET') <> '') Or (FieldVal(Side + '_PINS') <> '');
End;

Procedure CheckNetPair(A : String; B : String; Errs : TStringList);
Begin
    If (FieldVal(A) <> '') And (UpperCase(FieldVal(A)) = UpperCase(FieldVal(B))) Then
        Errs.Add(A + ' and ' + B + ' are the same net (' + FieldVal(A) + ').');
End;

{ Fills Names (suffixes) / Vals with what will be written and returns the
  role.  Errs block the write; Warns ask for confirmation. }
Function CollectAndValidate(Names : TStringList; Vals : TStringList;
                            Errs : TStringList; Warns : TStringList) : String;
Var
    Role, Key, Kind, Val, DesSide : String;
    I, J : Integer;
    Pins : TStringList;
Begin
    Role := CurrentRole(0);
    Result := Role;
    If Role = 'NONE' Then Exit;

    Names.Add('ROLE');
    Vals.Add(Role);

    Pins := TStringList.Create;
    Try
        For I := 0 To gFieldKeys.Count - 1 Do
        Begin
            Key  := gFieldKeys[I];
            Kind := gFieldKinds[I];
            Val  := Trim(gFieldCtrls.Items[I].Text);
            If Val = '' Then Continue;
            Names.Add(Key);
            Vals.Add(Val);

            { the *_DES side, if any, changes what nets / pins refer to }
            DesSide := '';
            If (Key = 'P_NET') Or (Key = 'P_PINS') Then DesSide := FieldVal('P_DES');
            If (Key = 'N_NET') Or (Key = 'N_PINS') Then DesSide := FieldVal('N_DES');

            If Kind = 'VALUE' Then
            Begin
                If Not ParseSI(Val) Then
                    Errs.Add(Key + ' = "' + Val + '" cannot be parsed by FYPA ' +
                             '(use forms like 100mA, 3V3, 0R01, 1.5E-9).')
                Else If (Key = 'R') And (gSIValue <= 0) Then
                    Errs.Add('PDN_R must be positive.')
                Else If (Key = 'REGULATOR_EFFICIENCY') And
                        ((gSIValue <= 0) Or (gSIValue > 1)) Then
                    Errs.Add('REGULATOR_EFFICIENCY must be in (0, 1], e.g. 0.9.')
                Else If (Key = 'QUIESCENT') And (gSIValue < 0) Then
                    Errs.Add('QUIESCENT must be >= 0.')
                Else If ((Key = 'I') Or (Key = 'GAIN')) And (gSIValue <= 0) Then
                    Warns.Add(Key + ' = ' + Val + ' is not positive.');
            End
            Else If (Kind = 'NET') And gNetsKnown And (DesSide = '') Then
            Begin
                If gNets.IndexOf(Val) < 0 Then
                    Warns.Add(Key + ': net "' + Val + '" is not on any pin of ' +
                              gDesignator + '.');
            End
            Else If (Kind = 'PINS') And (DesSide = '') Then
            Begin
                SplitPins(Val, Pins);
                For J := 0 To Pins.Count - 1 Do
                    If gPinNums.IndexOf(Pins[J]) < 0 Then
                        Warns.Add(Key + ': ' + gDesignator + ' has no pin "' + Pins[J] + '".');
            End;
        End;
    Finally
        Pins.Free;
    End;

    { --- required values --- }
    For I := 0 To gFieldKeys.Count - 1 Do
    Begin
        Key := gFieldKeys[I];
        If ((Key = 'V') Or (Key = 'I') Or (Key = 'R')) And (FieldVal(Key) = '') Then
            Errs.Add('PDN_' + Key + ' is required for ' + Role + '.');
    End;

    { --- terminals --- }
    If (Role = 'SOURCE') Or (Role = 'SINK') Then
    Begin
        If HasField('NET') Then
        Begin
            If (FieldVal('PINS') = '') And (FieldVal('NET') = '') Then
                Errs.Add('Single-net mode needs a Net or Pins.');
        End
        Else
        Begin
            If Not SideSet('P') Then Errs.Add('P side needs a net or pins.');
            If Not SideSet('N') Then Errs.Add('N side needs a net or pins.');
            If (FieldVal('P_DES') <> '') And (FieldVal('P_NET') = '') Then
                Errs.Add('P from parts needs a P net (it picks those parts'' pads on that net).');
            If (FieldVal('N_DES') <> '') And (FieldVal('N_NET') = '') Then
                Errs.Add('N from parts needs an N net (it picks those parts'' pads on that net).');
            CheckNetPair('P_NET', 'N_NET', Errs);
        End;
    End
    Else If (Role = 'SERIES') Or (Role = 'PATH') Then
    Begin
        If Not SideSet('P') And Not SideSet('N') Then
        Begin
            If (gPinNums.Count = 2) And (CurrentChanIdx(0) = '') Then
                Warns.Add('No nets given - FYPA infers them, but only for a single-channel ' +
                          'part with exactly two pads on two different nets.')
            Else
                Errs.Add(Role + ' needs P and N nets or pins (' + gDesignator + ' has ' +
                         IntToStr(gPinNums.Count) + ' pins' +
                         ', or this is an indexed channel).');
        End
        Else If Not SideSet('P') Or Not SideSet('N') Then
            Errs.Add('Give both P and N sides (net or pins), or leave both blank.');
        CheckNetPair('P_NET', 'N_NET', Errs);
    End
    Else If Role = 'REGULATOR' Then
    Begin
        If Not SideSet('OUT_P') Then Errs.Add('Out + needs a net or pins.');
        If Not SideSet('OUT_N') Then Errs.Add('Out - needs a net or pins.');
        If Not SideSet('IN_P')  Then Errs.Add('In + needs a net or pins.');
        If Not SideSet('IN_N')  Then Errs.Add('In - needs a net or pins.');
        CheckNetPair('OUT_P_NET', 'OUT_N_NET', Errs);
        CheckNetPair('IN_P_NET',  'IN_N_NET',  Errs);
        If (FieldVal('GAIN') = '') And (FieldVal('REGULATOR_TYPE') = '') Then
            Errs.Add('Set REGULATOR_TYPE (LDO or SMPS), or an explicit GAIN.');
        If (FieldVal('GAIN') <> '') And (FieldVal('REGULATOR_TYPE') <> '') Then
            Warns.Add('GAIN is set, so FYPA ignores REGULATOR_TYPE / efficiency.');
        If (UpperCase(FieldVal('REGULATOR_TYPE')) = 'LDO') And
           (FieldVal('REGULATOR_EFFICIENCY') <> '') Then
            Warns.Add('An LDO has gain 1; FYPA ignores REGULATOR_EFFICIENCY.');
        If (FieldVal('SMPS_TOPOLOGY') <> '') And
           (UpperCase(FieldVal('REGULATOR_TYPE')) <> 'SMPS') Then
            Warns.Add('SMPS_TOPOLOGY is set but REGULATOR_TYPE is not SMPS.');
    End;

    If (FieldVal('EXTRA_PINS') <> '') And (FieldVal('PINS_ONLY') = '') Then
        Warns.Add('EXTRA_PINS without PINS_ONLY becomes the whole allowlist - only those ' +
                  'pins will be used.');
End;


{ =======================================================================
  Writing parameters
  ======================================================================= }

{ Find the next parameter of channel Idx that must go: its suffix is not in
  Names, or it is a second copy of one that is. }
Function FindRemovable(Comp : ISch_Component; Idx : String; Names : TStringList) : ISch_Parameter;
Var
    Iter  : ISch_Iterator;
    Param : ISch_Parameter;
    Seen  : TStringList;
    Sfx   : String;
Begin
    Result := Nil;
    Seen := TStringList.Create;
    Iter := Comp.SchIterator_Create;
    Iter.AddFilter_ObjectSet(MkSet(eParameter));
    Try
        Param := Iter.FirstSchObject;
        While Param <> Nil Do
        Begin
            If ParamChannel(Param.Name) = Idx Then
            Begin
                Sfx := ParamSuffix(Param.Name);
                If (Names.IndexOf(Sfx) < 0) Or (Seen.IndexOf(Sfx) >= 0) Then
                Begin
                    Result := Param;
                    Break;
                End;
                Seen.Add(Sfx);
            End;
            Param := Iter.NextSchObject;
        End;
    Finally
        Comp.SchIterator_Destroy(Iter);
        Seen.Free;
    End;
End;

Procedure ApplyToPart(Comp : ISch_Component; Idx : String; Names : TStringList;
                      Vals : TStringList; Visible : Boolean);
Var
    Iter   : ISch_Iterator;
    Param  : ISch_Parameter;
    Done   : TStringList;
    Prefix, Sfx : String;
    I, J, NewCount : Integer;
Begin
    Prefix := ChanPrefix(Idx);
    Done := TStringList.Create;
    Try
        { 1. update the first copy of each key that stays }
        Iter := Comp.SchIterator_Create;
        Iter.AddFilter_ObjectSet(MkSet(eParameter));
        Try
            Param := Iter.FirstSchObject;
            While Param <> Nil Do
            Begin
                If ParamChannel(Param.Name) = Idx Then
                Begin
                    Sfx := ParamSuffix(Param.Name);
                    J := Names.IndexOf(Sfx);
                    If (J >= 0) And (Done.IndexOf(Sfx) < 0) Then
                    Begin
                        Done.Add(Sfx);
                        If (Param.Text <> Vals[J]) Or (Param.Name <> Prefix + Sfx) Then
                        Begin
                            SchServer.RobotManager.SendMessage(Param.I_ObjectAddress, c_BroadCast,
                                SCHM_BeginModify, c_NoEventData);
                            Param.Name     := Prefix + Sfx;
                            Param.Text     := Vals[J];
                            Param.IsHidden := Not Visible;
                            SchServer.RobotManager.SendMessage(Param.I_ObjectAddress, c_BroadCast,
                                SCHM_EndModify, c_NoEventData);
                            Inc(gChanged);
                        End;
                    End;
                End;
                Param := Iter.NextSchObject;
            End;
        Finally
            Comp.SchIterator_Destroy(Iter);
        End;

        { 2. remove the rest of the channel, one at a time, outside any iterator }
        Param := FindRemovable(Comp, Idx, Names);
        While Param <> Nil Do
        Begin
            Comp.RemoveSchObject(Param);
            SchServer.RobotManager.SendMessage(Comp.I_ObjectAddress, c_BroadCast,
                SCHM_PrimitiveRegistration, Param.I_ObjectAddress);
            Inc(gRemoved);
            Param := FindRemovable(Comp, Idx, Names);
        End;

        { 3. add keys the part did not have }
        NewCount := 0;
        For I := 0 To Names.Count - 1 Do
        Begin
            If Done.IndexOf(Names[I]) >= 0 Then Continue;
            Param := SchServer.SchObjectFactory(eParameter, eCreate_Default);
            If Param = Nil Then Continue;
            Inc(NewCount);
            Param.Name     := Prefix + Names[I];
            Param.Text     := Vals[I];
            Param.IsHidden := Not Visible;
            Param.Location := Point(Comp.Location.X,
                                    Comp.Location.Y - MilsToCoord(100 * NewCount));
            Comp.AddSchObject(Param);
            SchServer.RobotManager.SendMessage(Comp.I_ObjectAddress, c_BroadCast,
                SCHM_PrimitiveRegistration, Param.I_ObjectAddress);
            Inc(gAdded);
        End;
    Finally
        Done.Free;
    End;
End;

Procedure ApplyToDocument(Idx : String; Names : TStringList; Vals : TStringList;
                          Visible : Boolean);
Var
    Iter  : ISch_Iterator;
    Comp  : ISch_Component;
    Parts : TList;
    I     : Integer;
Begin
    gAdded := 0; gChanged := 0; gRemoved := 0; gParts := 0;

    { collect first - parts are modified outside the sheet iterator }
    Parts := TList.Create;
    Try
        Iter := gSch.SchIterator_Create;
        Iter.AddFilter_ObjectSet(MkSet(eSchComponent));
        Try
            Comp := Iter.FirstSchObject;
            While Comp <> Nil Do
            Begin
                If UpperCase(Comp.Designator.Text) = UpperCase(gDesignator) Then
                    Parts.Add(Comp);
                Comp := Iter.NextSchObject;
            End;
        Finally
            gSch.SchIterator_Destroy(Iter);
        End;

        SchServer.ProcessControl.PreProcess(gSch, '');
        Try
            For I := 0 To Parts.Count - 1 Do
            Begin
                ApplyToPart(Parts.Items[I], Idx, Names, Vals, Visible);
                Inc(gParts);
            End;
        Finally
            SchServer.ProcessControl.PostProcess(gSch, '');
        End;
    Finally
        Parts.Free;
    End;
    gSch.GraphicallyInvalidate;
End;

Function DoApply(Dummy : Integer) : Boolean;
Var
    Names, Vals, Errs, Warns : TStringList;
    Idx, Role, Msg : String;
    I : Integer;
Begin
    Result := False;
    Names := TStringList.Create;
    Vals  := TStringList.Create;
    Errs  := TStringList.Create;
    Warns := TStringList.Create;
    Try
        SaveFieldsToCache(0);
        Idx  := CurrentChanIdx(0);
        Role := CollectAndValidate(Names, Vals, Errs, Warns);

        If Errs.Count > 0 Then
        Begin
            Msg := 'Nothing was written - fix these first:' + #13#10 + #13#10;
            For I := 0 To Errs.Count - 1 Do Msg := Msg + '  - ' + Errs[I] + #13#10;
            If Warns.Count > 0 Then
            Begin
                Msg := Msg + #13#10 + 'Also:' + #13#10;
                For I := 0 To Warns.Count - 1 Do Msg := Msg + '  - ' + Warns[I] + #13#10;
            End;
            ShowMessage(Msg);
            Exit;
        End;

        If Role = 'NONE' Then
            Msg := 'Remove every ' + ChanPrefix(Idx) + '* parameter from ' + gDesignator + '?'
        Else If Warns.Count > 0 Then
        Begin
            Msg := 'Write anyway?' + #13#10 + #13#10;
            For I := 0 To Warns.Count - 1 Do Msg := Msg + '  - ' + Warns[I] + #13#10;
        End
        Else
            Msg := '';
        If Msg <> '' Then
            If Not ConfirmNoYes(Msg) Then Exit;

        ApplyToDocument(Idx, Names, Vals, FYPAParamForm.chkVisible.Checked);

        FYPAParamForm.lblStatus.Caption := gDesignator + '  ' + ChanPrefix(Idx) + '*:  ' +
            IntToStr(gAdded) + ' added, ' + IntToStr(gChanged) + ' changed, ' +
            IntToStr(gRemoved) + ' removed   (' + IntToStr(gParts) + ' part(s) on this sheet)';

        RefreshExisting(0);
        FillChannels(Idx);
        LoadChannel(0);
        Result := True;
    Finally
        Names.Free;
        Vals.Free;
        Errs.Free;
        Warns.Free;
    End;
End;


{ =======================================================================
  Form events
  ======================================================================= }
Procedure TFYPAParamForm.cbChannelChange(Sender : TObject);
Begin
    If gLoading Then Exit;
    FYPAParamForm.lblStatus.Caption := '';
    LoadChannel(0);
End;

Procedure TFYPAParamForm.cbRoleChange(Sender : TObject);
Begin
    If gLoading Then Exit;
    BuildFields(True);
End;

Procedure TFYPAParamForm.cbModeChange(Sender : TObject);
Begin
    If gLoading Then Exit;
    BuildFields(True);
End;

Procedure TFYPAParamForm.btnCompileClick(Sender : TObject);
Begin
    FYPAParamForm.lblStatus.Caption := 'Compiling project...';
    FYPAParamForm.Refresh;
    GatherPinsAndNets(True);
    FYPAParamForm.lblComp.Caption := 'Component: ' + gDesignator + '   (' +
        gComp.Comment.Text + ')   -   ' + IntToStr(gPinNums.Count) + ' pins';
    BuildFields(True);   { repopulates the net dropdowns, keeps typed values }
    If gNetsKnown Then
        FYPAParamForm.lblStatus.Caption := 'Nets refreshed (' + IntToStr(gNets.Count) + ' nets).'
    Else
        FYPAParamForm.lblStatus.Caption := 'Compile found no nets for ' + gDesignator +
            ' - is this sheet in the focused project?';
End;

Procedure TFYPAParamForm.btnApplyClick(Sender : TObject);
Begin
    DoApply(0);
End;

Procedure TFYPAParamForm.btnOKClick(Sender : TObject);
Begin
    If DoApply(0) Then FYPAParamForm.ModalResult := mrOK;
End;


{ =======================================================================
  Entry point
  ======================================================================= }
Procedure FYPA_PdnParams;
Var
    Iter     : ISch_Iterator;
    Comp     : ISch_Component;
    SelCount : Integer;
    Mixed    : Boolean;
    Cb       : TComboBox;
Begin
    If SchServer = Nil Then Exit;
    gSch := SchServer.GetCurrentSchDocument;
    If gSch = Nil Then
    Begin ShowMessage('No schematic document is currently open.'); Exit; End;
    If gSch.ObjectID = eSchLib Then
    Begin ShowMessage('Active document is a .SchLib. Please switch to a .SchDoc.'); Exit; End;

    { exactly one component - several parts of one designator also count }
    gComp    := Nil;
    SelCount := 0;
    Mixed    := False;
    Iter := gSch.SchIterator_Create;
    Iter.AddFilter_ObjectSet(MkSet(eSchComponent));
    Try
        Comp := Iter.FirstSchObject;
        While Comp <> Nil Do
        Begin
            If Comp.Selection Then
            Begin
                Inc(SelCount);
                If gComp = Nil Then gComp := Comp
                Else If UpperCase(Comp.Designator.Text) <> UpperCase(gComp.Designator.Text) Then
                    Mixed := True;
            End;
            Comp := Iter.NextSchObject;
        End;
    Finally
        gSch.SchIterator_Destroy(Iter);
    End;

    If SelCount = 0 Then
    Begin ShowMessage('No component selected. Please select a single schematic symbol.'); Exit; End;
    If Mixed Then
    Begin
        ShowMessage('Several different components are selected. Please select exactly one.');
        Exit;
    End;

    gDesignator := Trim(gComp.Designator.Text);

    gPinNums    := TStringList.Create;
    gNets       := TStringList.Create;
    gChanIdx    := TStringList.Create;
    gFieldKeys  := TStringList.Create;
    gFieldKinds := TStringList.Create;
    gFieldCtrls := TList.Create;
    gDynCtrls   := TList.Create;
    gValCache   := TStringList.Create;
    Try
        gNets.Sorted := True;
        gLoading := True;

        GatherPinsAndNets(False);

        FYPAParamForm.lblComp.Caption := 'Component: ' + gDesignator + '   (' +
            gComp.Comment.Text + ')   -   ' + IntToStr(gPinNums.Count) + ' pins';

        Cb := FYPAParamForm.cbRole;
        Cb.Items.Clear;
        Cb.Items.Add('SOURCE       - voltage source');
        Cb.Items.Add('SINK         - current load');
        Cb.Items.Add('SERIES       - resistance between two nets');
        Cb.Items.Add('REGULATOR    - LDO / SMPS');
        Cb.Items.Add('PATH         - external-FET SMPS switch path');
        Cb.Items.Add('(none)       - remove this channel''s parameters');

        Cb := FYPAParamForm.cbMode;
        Cb.Items.Clear;
        Cb.Items.Add('Single net      (PDN_NET / PDN_PINS)');
        Cb.Items.Add('Two terminal    (P / N nets or pins)');

        RefreshExisting(0);
        FillChannels('');
        gLoading := False;
        LoadChannel(0);

        FYPAParamForm.ShowModal;
    Finally
        ClearFields(0);
        gPinNums.Free;
        gNets.Free;
        gChanIdx.Free;
        gFieldKeys.Free;
        gFieldKinds.Free;
        gFieldCtrls.Free;
        gDynCtrls.Free;
        gValCache.Free;
    End;
End;
