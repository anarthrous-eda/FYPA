object FYPAParamForm: TFYPAParamForm
  Left = 0
  Top = 0
  BorderStyle = bsDialog
  Caption = 'FYPA - PDN Parameters'
  ClientHeight = 604
  ClientWidth = 860
  Color = clBtnFace
  Font.Charset = DEFAULT_CHARSET
  Font.Color = clWindowText
  Font.Height = -11
  Font.Name = 'Tahoma'
  Font.Style = []
  OldCreateOrder = False
  Position = poScreenCenter
  PixelsPerInch = 96
  TextHeight = 13
  object lblComp: TLabel
    Left = 8
    Top = 10
    Width = 480
    Height = 13
    AutoSize = False
    Caption = 'Component:'
    Font.Charset = DEFAULT_CHARSET
    Font.Color = clWindowText
    Font.Height = -11
    Font.Name = 'Tahoma'
    Font.Style = [fsBold]
    ParentFont = False
  end
  object lblChannel: TLabel
    Left = 8
    Top = 40
    Width = 44
    Height = 13
    Caption = 'Channel:'
  end
  object lblRole: TLabel
    Left = 8
    Top = 68
    Width = 25
    Height = 13
    Caption = 'Role:'
  end
  object lblMode: TLabel
    Left = 8
    Top = 96
    Width = 54
    Height = 13
    Caption = 'Terminals:'
  end
  object lblHelp: TLabel
    Left = 8
    Top = 122
    Width = 480
    Height = 44
    AutoSize = False
    Caption = ''
    Font.Charset = DEFAULT_CHARSET
    Font.Color = clNavy
    Font.Height = -11
    Font.Name = 'Tahoma'
    Font.Style = []
    ParentFont = False
    WordWrap = True
  end
  object lblStatus: TLabel
    Left = 8
    Top = 576
    Width = 470
    Height = 13
    AutoSize = False
    Caption = ''
    Font.Charset = DEFAULT_CHARSET
    Font.Color = clGreen
    Font.Height = -11
    Font.Name = 'Tahoma'
    Font.Style = []
    ParentFont = False
  end
  object cbChannel: TComboBox
    Left = 90
    Top = 36
    Width = 300
    Height = 21
    Style = csDropDownList
    ItemHeight = 13
    TabOrder = 0
    OnChange = cbChannelChange
  end
  object cbRole: TComboBox
    Left = 90
    Top = 64
    Width = 300
    Height = 21
    Style = csDropDownList
    ItemHeight = 13
    TabOrder = 1
    OnChange = cbRoleChange
  end
  object cbMode: TComboBox
    Left = 90
    Top = 92
    Width = 300
    Height = 21
    Style = csDropDownList
    ItemHeight = 13
    TabOrder = 2
    OnChange = cbModeChange
  end
  object sbFields: TScrollBox
    Left = 8
    Top = 170
    Width = 480
    Height = 372
    VertScrollBar.Tracking = True
    TabOrder = 3
  end
  object chkVisible: TCheckBox
    Left = 8
    Top = 552
    Width = 470
    Height = 17
    Caption = 'Show added / changed parameters on the sheet'
    TabOrder = 4
  end
  object gbPins: TGroupBox
    Left = 500
    Top = 4
    Width = 352
    Height = 300
    Caption = 'Component pins   (pin / name / net)'
    TabOrder = 5
    object lbPins: TListBox
      Left = 8
      Top = 18
      Width = 336
      Height = 242
      Font.Charset = DEFAULT_CHARSET
      Font.Color = clWindowText
      Font.Height = -11
      Font.Name = 'Consolas'
      Font.Style = []
      ItemHeight = 13
      ParentFont = False
      TabOrder = 0
    end
    object btnCompile: TButton
      Left = 8
      Top = 266
      Width = 336
      Height = 26
      Caption = 'Compile project && refresh nets'
      TabOrder = 1
      OnClick = btnCompileClick
    end
  end
  object gbExisting: TGroupBox
    Left = 500
    Top = 310
    Width = 352
    Height = 252
    Caption = 'PDN parameters currently on this part'
    TabOrder = 6
    object memoExisting: TMemo
      Left = 8
      Top = 18
      Width = 336
      Height = 226
      Font.Charset = DEFAULT_CHARSET
      Font.Color = clWindowText
      Font.Height = -11
      Font.Name = 'Consolas'
      Font.Style = []
      ParentFont = False
      ReadOnly = True
      ScrollBars = ssBoth
      TabOrder = 0
      WordWrap = False
    end
  end
  object btnApply: TButton
    Left = 572
    Top = 570
    Width = 88
    Height = 28
    Caption = 'Apply'
    TabOrder = 7
    OnClick = btnApplyClick
  end
  object btnOK: TButton
    Left = 668
    Top = 570
    Width = 88
    Height = 28
    Caption = 'OK'
    Default = True
    TabOrder = 8
    OnClick = btnOKClick
  end
  object btnClose: TButton
    Left = 764
    Top = 570
    Width = 88
    Height = 28
    Cancel = True
    Caption = 'Close'
    ModalResult = 2
    TabOrder = 9
  end
end
