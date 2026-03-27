import pickle
import os

path = os.path.join("local_runner", "cache", "universe_ohlcv_5yr.pkl")
with open(path, "rb") as f:
    d = pickle.load(f)

bc = [len(df) for df in d.values()]
print("Local 5yr cache:", len(d), "tickers")
print("Bar range:", min(bc), "-", max(bc))
print("File size:", round(os.path.getsize(path) / 1024 / 1024), "MB")
