.PHONY: deploy deploy-cam bring-agent-back


deploy:
	git ls-files | rsync -azP --files-from=- . csft:kasl
	ssh csft "cd kasl && PYTHON_KEYRING_BACKEND=keyring.backends.null.Keyring /home/diego/.local/bin/poetry install"

deploy-cam:
	git ls-files | rsync -azP --files-from=- . cam:rds/hpc-work/kasl
	#ssh cam "cd rds/hpc-work/kasl && PYTHON_KEYRING_BACKEND=keyring.backends.null.Keyring poetry install"

bring-agents-back:
	rsync -azPr "csft:kasl/models/blind_three_goals_rgb_channel_reg/*" models/blind_three_goals_rgb_channel_reg

requirements.txt: pyproject.toml
	poetry export -f requirements.txt -o requirements.txt --without-hashes
