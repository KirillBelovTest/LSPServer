
import os
import sys
import re
import subprocess
import argparse
import errno

def main():
	"""Wrap around a WolframKernel process and convert LSP traffic to a format that WolframKernel can handle.
	
	WolframKernel is used as a LSP Server, but WolframKernel cannot read arbitrary data from stdin.
	bug 11114
	so this thin Python script is used to marshall stdio data between an LSP client and the WolframKernel,
	which is an LSP server.

	The LSP client starts this Python script with the appropriate arguments.

	The process tree looks like this:
	LSP Client (Sublime Text, VS Code, Atom, etc.)
	  |
	  Python interpreter running this script
	    |
	    WolframKernel

	NOTE: Currently 0 or 1 lines are expected from the kernel for each loop. More than 1 line are not handled currently.
	For example, sending more than 1 notification at a time is not currently supported.

	All data is assumed to be UTF-8.

	Works with Python 2 and Python 3.
	"""

	parser = argparse.ArgumentParser()
	parser.add_argument('wolframkernel', help='path to WolframKernel')
	parser.add_argument('--logDir', help='directory for log files', type=str)
	args = parser.parse_args()


	logDir = args.logDir

	if logDir:
		if not os.path.isdir(logDir):
			# try to create
			os.makedirs(logDir)
			if not os.path.isdir(logDir):
				raise FileNotFoundError(errno.ENOENT, os.strerror(errno.ENOENT), logDir)
		logFileName = os.path.join(logDir, 'logFile.txt')

		if sys.version_info[0] >= 3:
			logFile = open(logFileName, 'w', encoding='utf-8')
		else:
			logFile = open(logFileName, 'w')

		kernelLogFile = os.path.join(logDir, 'kernelLogFile.txt')

		if os.path.exists(kernelLogFile):
			os.remove(kernelLogFile)

		# Assume LSPServer paclet is already installed
		# python 3: f'(Needs["LSPServer`"];LSPServer`StartServer[{stringEscape(kernelLogFile)}])'
		runString = '(Catch[ $Messages = Streams["stderr"]; Needs["LSPServer`"]; LSPServer`StartServer[' + stringEscape(kernelLogFile) + '], _, Exit[3]& ])'

		debug = True
	else:
		runString = '(Catch[ $Messages = Streams["stderr"]; Needs["LSPServer`"]; LSPServer`StartServer[], _, Exit[3]& ])'

		debug = False


	if debug:
		logFile.write(str(sys.version_info) + '\n')
		logFile.write(str(sys.argv) + '\n')
		logFile.write('\n')
		logFile.flush()


	wolframkernel = args.wolframkernel
	if sys.platform == "win32":
		base = os.path.basename(wolframkernel)
		if base.lower() == 'wolframkernel.exe' or base.lower() == 'wolframkernel':
			dir = os.path.dirname(wolframkernel)
			wolframkernel = os.path.join(dir, 'wolfram.exe')
			logFile.write('Silently converting from using WolframKernel.exe to using wolfram.exe\n')
			logFile.write('WolframKernel.exe cannot be used because it opens a separate window and hangs on stdin.\n')
			logFile.flush()


	if not os.path.isfile(wolframkernel):
		raise FileNotFoundError(errno.ENOENT, os.strerror(errno.ENOENT), wolframkernel)

	#
	# this means that with Python 2, that stderr is never read from the kernel
	#
	if sys.version_info[0] >= 3:
		stderr_strategy = subprocess.DEVNULL
	else:
		stderr_strategy = subprocess.PIPE


	kernelProc = subprocess.Popen(
	    [wolframkernel,
	    		# -noprompt to prevent the standard banner and
	    		# In[] / Out[] prompts interfering with protocol
	    		'-noprompt',
	    		# -rawterm is needed to enable $PreRead
	    		# bug 337831
	    		'-rawterm',
	    		'-run', runString],
	    stdin=subprocess.PIPE,
	    stdout=subprocess.PIPE,
	    stderr=stderr_strategy
	)


	#
	# Setup stdin and stdout to correctly handle \r\n
	# https://stackoverflow.com/a/38939320
	#
	if sys.version_info[0] >= 3:
		proxy_stdin = sys.stdin.buffer
		proxy_stdout = sys.stdout.buffer
	else:
		# Python 2 on Windows opens sys.stdin in text mode, and
		# binary data that read from it becomes corrupted on \r\n
		if sys.platform == "win32":
			# set sys.stdin to binary mode
			import msvcrt
			msvcrt.setmode(sys.stdin.fileno(), os.O_BINARY)
			msvcrt.setmode(sys.stdout.fileno(), os.O_BINARY)
		proxy_stdin = sys.stdin
		proxy_stdout = sys.stdout


	# Start the stdio loop with parent client and child kernel
	while True:

		headerBytes = proxy_stdin.readline()
		if sys.version_info[0] >= 3:
			headerString = headerBytes.decode('utf-8')
		else:
			headerString = headerBytes

		m = re.search('Content-Length: (\d+)', headerString);
		if not m:
			break
		contentLength = int(m.group(1))

		# read the \r\n
		proxy_stdin.read(2)

		if debug:
			logFile.write('C-->P  ' + str(contentLength) + '\n')
			logFile.flush()

		contentBytes = proxy_stdin.read(contentLength)
		if sys.version_info[0] >= 3:
			contentString = contentBytes.decode('utf-8')
		else:
			contentString = contentBytes

		if debug:
			logFile.write('C-->P  ' + contentString + '\n')
			logFile.flush()

		if len(contentString) != contentLength:
			if debug:
				logFile.write('C-->P  actual content length: ' + str(len(contentString)) + '\n')
				logFile.flush()
			break

		contentString+='\n'

		contentBytes = contentString.encode('utf-8')
		kernelProc.stdin.write(contentBytes)
		kernelProc.stdin.flush()
		contentBytes = kernelProc.stdout.readline()

		# Did the kernel die?
		if kernelProc.poll():
			break

		#
		# contentBytes is b'' or b'\x0a'
		#
		# Null response from kernel
		#
		# Something like a notification was sent to the kernel, and we do not need to send a response back to the client
		#
		if len(contentBytes) <= 1:
			if debug:
				logFile.write('P<--K  null\n')
				logFile.flush()
			continue

		if debug:
			contentString = contentBytes.decode('utf-8')
			logFile.write('P<--K  ' + contentString + '\n')
			logFile.flush()

		proxy_stdout.write(('Content-Length: ' + str(len(contentBytes)) + '\r\n').encode('utf-8'))
		proxy_stdout.write('\r\n'.encode('utf-8'))
		proxy_stdout.write(contentBytes)
		proxy_stdout.flush()

		if debug:
			logFile.write('loop\n\n')
			logFile.flush()

	# Make sure that child kernel is killed before exiting
	try:
		kernelProc.kill()
	except OSError:
		pass
	kernelProc.wait()
	if debug:
		logFile.write('kernel exit code: ' + str(kernelProc.returncode) + '\n')
		logFile.flush()


def stringEscape(s):
	"""Return a string appropriate for passing into WolframKernel."""
	return '"' + s.replace('\\', '\\\\') + '"'


if __name__== '__main__':
	main()

