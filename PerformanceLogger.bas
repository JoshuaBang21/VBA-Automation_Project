Option Explicit

Public Sub LogSummaryPerformance(ByVal macroName As String, _
                                ByVal startTime As Double, _
                                ByVal processedRows As Long, _
                                ByVal summaryRows As Long)

    Dim ws As Worksheet
    Dim lastRow As Long
    Dim runtimeSec As Double

    On Error Resume Next
    Set ws = ThisWorkbook.Worksheets("Log")
    On Error GoTo 0

    If ws Is Nothing Then
        Set ws = ThisWorkbook.Worksheets.Add(After:=ThisWorkbook.Worksheets(ThisWorkbook.Worksheets.Count))
        ws.Name = "Log"
        ws.Range("A1:E1").Value = Array("Timestamp", "Macro", "RuntimeSec", "ProcessedRows", "SummaryRows")
        ws.Range("A1:E1").Font.Bold = True
        ws.Columns("A:E").AutoFit
    End If

    runtimeSec = Round(Timer - startTime, 3)

    lastRow = ws.Cells(ws.Rows.Count, "A").End(xlUp).Row + 1

    ws.Cells(lastRow, 1).Value = Now
    ws.Cells(lastRow, 2).Value = macroName
    ws.Cells(lastRow, 3).Value = runtimeSec
    ws.Cells(lastRow, 4).Value = processedRows
    ws.Cells(lastRow, 5).Value = summaryRows

    ws.Columns("A:E").AutoFit

End Sub
