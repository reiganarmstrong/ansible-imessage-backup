.PHONY: activate-nextcloud archive backup check install-schedule restore-test retire-local retire-remote-superseded status test uninstall-schedule

activate-nextcloud:
	ansible-playbook activate-nextcloud.yml

archive:
	ansible-playbook archive.yml

backup:
	ansible-playbook playbook.yml

check:
	ansible-playbook --syntax-check activate-nextcloud.yml
	ansible-playbook --syntax-check activate-nextcloud-preflight.yml
	ansible-playbook --syntax-check archive.yml
	ansible-playbook --syntax-check playbook.yml
	ansible-playbook --syntax-check catalog-hydrate.yml
	ansible-playbook --syntax-check retire-local-archives.yml
	ansible-playbook --syntax-check retire-superseded-remote-archives.yml
	ansible-playbook --syntax-check catalog.yml
	ansible-playbook --syntax-check pruning-report.yml
	ansible-playbook --syntax-check nextcloud-archive.yml
	ansible-playbook --syntax-check nextcloud-backlog.yml
	ansible-playbook --syntax-check nextcloud-metadata.yml
	ansible-playbook --syntax-check restore-test.yml
	ansible-playbook --syntax-check status.yml
	ansible-playbook --syntax-check install-schedule.yml
	ansible-playbook --syntax-check uninstall-schedule.yml
	python3 -m unittest discover --start-directory tests --verbose

install-schedule:
	ansible-playbook install-schedule.yml

restore-test:
	ansible-playbook restore-test.yml

retire-local:
	ansible-playbook retire-local-archives.yml

retire-remote-superseded:
	ansible-playbook retire-superseded-remote-archives.yml

status:
	ansible-playbook status.yml

test:
	python3 -m unittest discover --start-directory tests --verbose

uninstall-schedule:
	ansible-playbook uninstall-schedule.yml
