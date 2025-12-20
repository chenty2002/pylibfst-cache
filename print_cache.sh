python cache/tllog_parser.py $1 $2 | sort -k 1 -n | tee tl.log
python cache/tllog_visual.py tl.log
