from xtquant import xtdata
import inspect

def list_xtdata_api():
    print("--- xtdata API ---")
    for name, obj in inspect.getmembers(xtdata):
        if not name.startswith("__"):
            print(name)

if __name__ == "__main__":
    list_xtdata_api()
