# models/ — trained model sets

One folder per model set, written by tcview's **Train** tab (or by the
trainers' `--out`). A set is the models of one detector, stage by stage:

| File | Stage | Written by |
|---|---|---|
| `lut_model.{bin,h,json}` | 1, colour table | `host/train/train_lut.py` |
| `clf_model.{bin,h,json}` | 2, blob classifier, trained behind this folder's table | `host/train/train_clf.py` |
| `cnn_model.{bin,h,json}` | 3, crop CNN (phase P7) | `host/train/train_cnn.py` |
| `*_log.txt` | | what the trainer printed |

`.bin` is the model record the camera checks at boot, `.h` the same bytes as
the C array the firmware compiles in, `.json` how it was made (sessions,
parameters, train ids). A classifier or CNN is valid only behind the table
whose train id its `.json` records (`lut_id`), which is why it lives in that
table's folder; training stage 2 on table X makes a new set holding a copy of
X plus the classifier.

`main/models/` is the set the firmware compiles in. Only tcview's **Firmware**
tab writes it: it moves the set that was there into this folder
(`firmware_<lut_id>_<date>`) and copies the chosen one in. The plan and the
reasons are in `RECOGNITION.md` at the repository root.
