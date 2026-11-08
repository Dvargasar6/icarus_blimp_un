# Builds and runs the detector's verification suites on this PC.
#
# The detector sources have no ESP-IDF dependency precisely so this is
# possible: the code that will run on the camera is tested here against
# synthetic frames whose answer we chose. AddressSanitizer is on because test 9
# drives the label arrays to their worst case, and an overrun there must fail
# loudly.

CFLAGS := -O1 -g -Wall -Wextra -Wconversion -std=c11 -fsanitize=address,undefined
LDLIBS := -lm

# Every portable source the camera runs. The tests and the viewer's library
# all build from this one list, so a new stage cannot be left out of one.
SRC := ../main/blob_detect.c ../main/tc_pipeline.c ../main/color_lut.c ../main/blob_feat.c \
       ../main/blob_clf.c ../main/blob_cnn.c
HDR := ../main/blob_detect.h ../main/tc_pipeline.h ../main/color_lut.h ../main/blob_feat.h \
       ../main/blob_clf.h ../main/blob_cnn.h

.PHONY: all test lib clean
all: test_blob_detect test_pipeline test_blob_feat lib

test_blob_detect: test_blob_detect.c $(SRC) $(HDR)
	$(CC) $(CFLAGS) -o $@ test_blob_detect.c $(SRC) $(LDLIBS)

test_pipeline: test_pipeline.c $(SRC) $(HDR)
	$(CC) $(CFLAGS) -o $@ test_pipeline.c $(SRC) $(LDLIBS)

test_blob_feat: test_blob_feat.c $(SRC) $(HDR)
	$(CC) $(CFLAGS) -o $@ test_blob_feat.c $(SRC) $(LDLIBS)

test: test_blob_detect test_pipeline test_blob_feat
	./test_blob_detect
	./test_pipeline
	./test_blob_feat

# The same detector as a shared library, for the PC viewer (viewer/tcview/
# detector.py loads it with ctypes). No sanitizers: they cannot be loaded
# into a Python process. The viewer rebuilds it when a C source is newer.
lib: libblob_detect.so

libblob_detect.so: $(SRC) $(HDR)
	$(CC) -O2 -Wall -Wextra -std=c11 -fPIC -shared -o $@ $(SRC) $(LDLIBS)

clean:
	rm -f test_blob_detect test_pipeline test_blob_feat libblob_detect.so
