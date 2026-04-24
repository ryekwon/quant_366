try:
    val = get_etf_iopv("510050.SH")
    print(f"Global get_etf_iopv exists! Value: {val}")
except NameError:
    print("Global get_etf_iopv does NOT exist.")
except Exception as e:
    print(f"Error calling get_etf_iopv: {e}")
