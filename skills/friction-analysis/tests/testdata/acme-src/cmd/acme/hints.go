package main

// hints printed after commands; agents copy these verbatim

const noteHint = "Record what you did: run acme log --role user --content <text>"

func publishHint() string {
	return "Run `acme notes sync` to publish your notes."
}

func listHint() string {
	return "acme plan list --json"
}

func aboutHint() string {
	return "acme is the command line for plans and sessions"
}
