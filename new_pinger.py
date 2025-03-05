#!/usr/bin/env python3
import sys
import os
import socket
import time
import subprocess
import re
import ipaddress
import yaml
from concurrent.futures import ThreadPoolExecutor, as_completed

# ANSI color definitions
BOLD    = "\033[1m"
RESET   = "\033[0m"
YELLOW  = "\033[33m"
RED     = "\033[31m"
GREEN   = "\033[32m"

def clear_screen():
    """Clear the terminal screen."""
    print("\033[H\033[J", end="")

def format_status(s, width=10):
    """
    Pad a string s to a fixed width (ignoring ANSI escape sequences)
    so that columns line up.
    """
    clean = re.sub(r'\x1B\[[0-?]*[ -/]*[@-~]', '', s)
    padding = width - len(clean)
    return (" " * padding + s) if padding > 0 else s

class TestResult:
    """
    Represents one test (ICMP or TCP) for a host.
    Maintains a fixed-length history of measurements.
    """
    def __init__(self, protocol, port=None, history_length=35):
        self.protocol = protocol.upper()  # "ICMP" or "TCP"
        self.port = port                  # For TCP tests; None for ICMP
        self.history = ["."] * history_length
        self.latency = -1                 # in milliseconds (-1 means no response)
        self.last_seen = "Last seen: " + time.strftime("%c")
        self.service = ""                 # For TCP: "open" or "closed"

    def update_history(self, index, symbol):
        self.history[index] = symbol

    def get_history_string(self, current_index, history_length):
        """Return the history chart as a string (oldest to newest)."""
        s = ""
        for i in range(history_length):
            idx = (current_index - i + history_length) % history_length
            s += self.history[idx]
        return s

class Host:
    """
    Represents a host with an IP, description, and one or more tests.
    """
    def __init__(self, ip, description, tests, history_length=35):
        try:
            ipaddress.IPv4Address(ip)
        except ipaddress.AddressValueError:
            sys.exit(f"Invalid IP address: {ip}")
        self.ip = ip
        self.description = description
        # If no tests are provided, default to a single ICMP test.
        self.tests = tests if tests else [TestResult("ICMP", history_length=history_length)]
        self.history_length = history_length

class MultiPing:
    """
    Main class: loads hosts from a YAML config file, runs tests in parallel,
    and displays results in organized columns.
    """
    def __init__(self, config_file, history_length=35):
        self.config_file = config_file
        self.history_length = history_length
        self.current_index = history_length - 1
        self.hosts = []
        self.load_config()

    def load_config(self):
        """Load hosts from a YAML file with the specified structure."""
        if not os.path.exists(self.config_file):
            sys.exit(f"Config file not found: {self.config_file}")
        with open(self.config_file, "r") as f:
            try:
                data = yaml.safe_load(f)
            except Exception as e:
                sys.exit(f"Error parsing YAML file: {e}")
        if "hosts" not in data:
            sys.exit("YAML file must contain a 'hosts' key.")
        self_ips = []
        if "ignore_self" in data:
            self_ips = subprocess.check_output(["hostname", "-I"]).decode().strip().split()
        for host_item in data["hosts"]:
            # Each item is a dict with a single key: the IP address.
            for ip, details in host_item.items():
                if ip in self_ips:
                    continue
                description = details.get("description", ip)
                tests = []
                test_list = details.get("tests", None)
                if test_list:
                    for test in test_list:
                        protocol = test.get("protocol", "ICMP").upper()
                        if protocol == "TCP":
                            port_val = test.get("port")
                            if port_val is None:
                                sys.exit(f"TCP test for {ip} must specify a port.")
                            # Expand port ranges if needed.
                            if isinstance(port_val, str) and "-" in port_val:
                                try:
                                    start, end = port_val.split("-")
                                    for p in range(int(start), int(end) + 1):
                                        tests.append(TestResult("TCP", p, self.history_length))
                                except Exception as e:
                                    sys.exit(f"Error expanding port range '{port_val}' for {ip}: {e}")
                            else:
                                try:
                                    port_num = int(port_val)
                                    tests.append(TestResult("TCP", port_num, self.history_length))
                                except Exception as e:
                                    sys.exit(f"Error converting port '{port_val}' for {ip}: {e}")
                        else:
                            tests.append(TestResult("ICMP", history_length=self.history_length))
                else:
                    tests.append(TestResult("ICMP", history_length=self.history_length))
                self.hosts.append(Host(ip, description, tests, self.history_length))

    @staticmethod
    def run_icmp_test(ip):
        """
        Run an ICMP test using the ping command with a 1-second timeout.
        Returns (up, latency_in_ms) using the average round-trip time.
        """
        cmd = ["ping", "-c", "1", "-W", "1", ip]
        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            stdout, _ = proc.communicate(timeout=0.5)
        except Exception:
            return False, None
        up = proc.returncode == 0
        latency = None
        if up:
            # Match the round-trip statistics line:
            # e.g., "round-trip min/avg/max/stddev = 15.086/15.086/15.086/0.000 ms"
            regex = r"([\d\.]+)/([\d\.]+)/([\d\.]+)/((?:[\d\.]+|nan))\s*ms"
            m = re.search(regex, stdout)
            if m:
                try:
                    avg_latency_str = m.group(2)
                    if avg_latency_str.lower() == "nan":
                        latency = None
                    else:
                        latency = float(avg_latency_str)
                except Exception:
                    latency = None
        return up, latency

    @staticmethod
    def run_tcp_test(ip, port):
        """Run a TCP test by attempting a connection; return (open, latency_in_ms)."""
        start_time = time.time()
        try:
            sock = socket.create_connection((ip, port), timeout=0.5)
            sock.close()
            latency = (time.time() - start_time) * 1000
            return True, latency
        except Exception:
            return False, None

    @staticmethod
    def symbol_for_latency(latency):
        """Return a symbol based on latency (in ms)."""
        if latency is None:
            return "."
        elif latency < 10:
            return "."
        elif latency < 100:
            return f"{BOLD}{YELLOW}o{RESET}"
        else:
            return "O"

    def update_tests(self):
        """Run all tests in parallel and update their history slot."""
        # First, mark the current slot as failure ("X") for all tests.
        for host in self.hosts:
            for test in host.tests:
                test.update_history(self.current_index, "X")
                test.latency = -1

        # Prepare to run all tests concurrently.
        tasks = []
        with ThreadPoolExecutor(max_workers=20) as executor:
            for host in self.hosts:
                ip = host.ip
                for test in host.tests:
                    if test.protocol == "ICMP":
                        future = executor.submit(self.run_icmp_test, ip)
                        tasks.append((host, test, future))
                    elif test.protocol == "TCP":
                        future = executor.submit(self.run_tcp_test, ip, test.port)
                        tasks.append((host, test, future))
            # Process the results as they complete.
            for host, test, future in tasks:
                try:
                    result = future.result(timeout=0.5)
                except Exception:
                    result = (False, None)
                up, latency = result
                if test.protocol == "ICMP":
                    if up:
                        test.latency = latency if latency is not None else 0
                        symbol = self.symbol_for_latency(latency)
                        test.update_history(self.current_index, symbol)
                        test.last_seen = ""
                    else:
                        test.latency = -1
                        if test.last_seen == "":
                            test.last_seen = "Last seen: " + time.strftime("%c")
                        test.update_history(self.current_index, "X")
                elif test.protocol == "TCP":
                    if up:
                        test.latency = latency if latency is not None else 0
                        symbol = self.symbol_for_latency(latency)
                        test.update_history(self.current_index, symbol)
                        test.last_seen = ""
                        test.service = "open"
                    else:
                        test.latency = -1
                        if test.last_seen == "":
                            test.last_seen = "Last seen: " + time.strftime("%c")
                        test.update_history(self.current_index, "X")
                        test.service = "closed"

    def display_results(self):
        """Clear the screen and display test results in organized columns."""
        clear_screen()
        print(f"{BOLD}\n\nMultiPing NG - {RESET}{time.strftime('%c')}\n")
        for host in self.hosts:
            print(f"{BOLD}{host.description:<20}{RESET} ({host.ip})")
            header = f"{'Test':<15} {'Status':>10}   {'History':<35}  {'Last Seen'}"
            print("    " + header)
            for test in host.tests:
                if test.protocol == "ICMP":
                    label = "ICMP"
                    status_plain = "DOWN" if test.latency == -1 else f"{test.latency:.1f}ms"
                    status = f"{BOLD}{RED}DOWN{RESET}" if test.latency == -1 else status_plain
                elif test.protocol == "TCP":
                    label = f"TCP port {test.port}"
                    status_plain = "DOWN" if test.latency == -1 else f"{test.latency:.1f}ms"
                    status = f"{BOLD}{RED}DOWN{RESET}" if test.latency == -1 else status_plain
                history = test.get_history_string(self.current_index, self.history_length)
                status_formatted = format_status(status, 10)
                row = f"{label:<15} {status_formatted}   {history:<35}  {test.last_seen}"
                print("    " + row)
            print()

    def run(self):
        """Continuously run tests, update history, and display results."""
        while True:
            self.update_tests()
            self.display_results()
            self.current_index -= 1
            if self.current_index < 0:
                self.current_index = self.history_length - 1
            time.sleep(1)

if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit("Usage: new_pinger.py <config.yaml>")
    try:
        multiping = MultiPing(sys.argv[1])
        multiping.run()
    except KeyboardInterrupt:
        print("\nExiting...")
        sys.exit(0) 
