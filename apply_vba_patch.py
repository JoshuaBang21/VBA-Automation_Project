import os
import sys
import win32com.client

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_WORKBOOK_PATH = os.path.join(BASE_DIR, "20260119_bts26_GG_PO.xlsm")

PERFORMANCE_LOGGER_CODE = open(os.path.join(BASE_DIR, "PerformanceLogger.bas"), "r", encoding="utf-8").read()
MODULE1_CODE = open(os.path.join(BASE_DIR, "Module1.bas"), "r", encoding="utf-8").read()

WORKBOOK_PATH = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_WORKBOOK_PATH

xl = win32com.client.DispatchEx("Excel.Application")
xl.Visible = False
xl.DisplayAlerts = False
xl.ScreenUpdating = False
xl.AutomationSecurity = 1

try:
    wb = xl.Workbooks.Open(WORKBOOK_PATH, ReadOnly=False)
    vbp = wb.VBProject

    for component in list(vbp.VBComponents):
        if component.Name == "PerformanceLogger":
            vbp.VBComponents.Remove(component)
            break

    performance_logger = vbp.VBComponents.Add(1)
    performance_logger.Name = "PerformanceLogger"
    performance_logger.CodeModule.AddFromString(PERFORMANCE_LOGGER_CODE)

    module1 = None
    for component in list(vbp.VBComponents):
        if component.Name == "Module1":
            module1 = component
            break

    if module1 is None:
        module1 = vbp.VBComponents.Add(1)
        module1.Name = "Module1"

    module1.CodeModule.DeleteLines(1, module1.CodeModule.CountOfLines)
    module1.CodeModule.AddFromString(MODULE1_CODE)

    wb.Save()
    wb.Close(SaveChanges=True)
    print(f"VBA patch applied successfully to {WORKBOOK_PATH}.")
finally:
    xl.ScreenUpdating = True
    xl.Quit()
