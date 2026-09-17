Option Explicit

Sub Final_Style_Summary_With_Formatting()
    Dim wsRaw As Worksheet, wsRes As Worksheet
    Dim dict As Object
    Dim i As Long, lastRow As Long
    Dim style As String, category As String, units As Double
    Dim processedRows As Long
    Dim summaryRows As Long
    Dim startTime As Double

    startTime = Timer

    ' 1. 설정
    Set wsRaw = ActiveSheet
    Set dict = CreateObject("Scripting.Dictionary")

    ' 2. 원본 데이터 범위 파악 (A:Style No, C:구분, E:Units)
    lastRow = wsRaw.Cells(wsRaw.Rows.Count, "A").End(xlUp).Row

    ' 3. 데이터 취합 (스타일별 합계 누적)
    For i = 2 To lastRow
        processedRows = processedRows + 1

        style = Trim(CStr(wsRaw.Cells(i, 1).Value))
        category = Trim(wsRaw.Cells(i, 3).Value)
        units = Val(wsRaw.Cells(i, 5).Value)

        If style <> "" Then
            If Not dict.Exists(style) Then
                dict.Add style, Array(0#, 0#)
            End If

            Dim temp As Variant
            temp = dict(style)
            If category = "GG" Then
                temp(0) = temp(0) + units
            ElseIf category = "PO" Then
                temp(1) = temp(1) + units
            End If
            dict(style) = temp
        End If
    Next i

    ' 4. 결과 시트 생성 또는 초기화
    On Error Resume Next
    Set wsRes = Sheets("Final_Summary")
    On Error GoTo 0
    If wsRes Is Nothing Then
        Set wsRes = Sheets.Add(After:=wsRaw)
        wsRes.Name = "Final_Summary"
    Else
        wsRes.Cells.Clear
    End If

    ' 5. 헤더 작성 (D열 제목 정정 반영)
    wsRes.Range("A1:D1").Value = Array("Style No.", "GG Total", "PO Total", "GG-PO, Min 0")

    ' 6. 데이터 출력 및 총계 계산
    Dim k As Variant, r As Long
    Dim totalGG As Double, totalPO As Double, totalResult As Double
    r = 2

    For Each k In dict.Keys
        Dim ggSum As Double, poSum As Double, diff As Double
        ggSum = dict(k)(0)
        poSum = dict(k)(1)

        ' 마이너스 0 처리 로직
        diff = ggSum - poSum
        If diff < 0 Then diff = 0

        wsRes.Cells(r, 1).Value = k
        wsRes.Cells(r, 2).Value = ggSum
        wsRes.Cells(r, 3).Value = poSum
        wsRes.Cells(r, 4).Value = diff

        totalGG = totalGG + ggSum
        totalPO = totalPO + poSum
        totalResult = totalResult + diff
        r = r + 1
    Next k

    summaryRows = r - 2

    ' 7. TOTAL 행 작성
    wsRes.Cells(r, 1).Value = "TOTAL"
    wsRes.Cells(r, 2).Value = totalGG
    wsRes.Cells(r, 3).Value = totalPO
    wsRes.Cells(r, 4).Value = totalResult

    ' 8. 서식 설정 (핵심 추가 사항)
    ' A열 전체 중앙 정렬
    wsRes.Columns("A").HorizontalAlignment = xlCenter

    ' B, C, D열 천 단위 구분 기호 (숫자 서식)
    wsRes.Columns("B:D").NumberFormat = "#,##0"

    ' 헤더 및 토탈 행 강조
    With wsRes.Range("A1:D1")
        .Interior.Color = RGB(220, 230, 241)
        .Font.Bold = True
        .HorizontalAlignment = xlCenter ' 헤더는 모두 중앙 정렬
    End With

    With wsRes.Range(wsRes.Cells(r, 1), wsRes.Cells(r, 4))
        .Interior.Color = RGB(255, 255, 0)
        .Font.Bold = True
    End With

    ' 테두리 및 열 너비 자동 조정
    wsRes.Range("A1:D" & r).Borders.LineStyle = xlContinuous
    wsRes.Columns("A:D").AutoFit

    PerformanceLogger.LogSummaryPerformance "Final_Style_Summary_With_Formatting", startTime, processedRows, summaryRows

    MsgBox "서식 적용이 완료된 요약 보고서가 생성되었습니다!", vbInformation
End Sub
