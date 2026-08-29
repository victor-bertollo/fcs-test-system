import yaml
from itertools import product

data = {
    "chip": "74HC157 MUX",
    "package": "DIP16",
    "stop_on_fail": True,
    "tests": []
}

for a_or_b in (0, 1):
    for s in product("01", repeat=8):
        s = "".join(s)
        frame = str(a_or_b)
        frame += s[0:2]
        frame += "z"
        frame += s[2:4]
        frame += "z"
        frame += "g"
        frame += "z"
        frame += s[5:3:-1]
        frame += "z"
        frame += s[7:5:-1]
        frame += "gv"

        expect = ["x"] * 16
        expect[3] = s[a_or_b]
        expect[6] = s[2 + a_or_b]
        expect[8] = s[4 + a_or_b]
        expect[11] = s[6 + a_or_b]
        
        data["tests"].append({"name": s, "frame": frame, "expect": "".join(expect)})

with open("configs/mux.yaml", "w") as file:
    yaml.dump(data, file)

