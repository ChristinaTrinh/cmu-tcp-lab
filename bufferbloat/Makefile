TOP_DIR = .
INC_DIR = $(TOP_DIR)/inc
SRC_DIR = $(TOP_DIR)/src
BUILD_DIR = $(TOP_DIR)/build
CC=gcc
FLAGS = -pthread -fPIC -g -ggdb -pedantic -Wall -Wextra -DDEBUG -I$(INC_DIR)
OBJS = $(BUILD_DIR)/cmu_packet.o $(BUILD_DIR)/cmu_tcp.o $(BUILD_DIR)/backend.o

all: server client tests/testing_server exp1_server exp1_client

$(BUILD_DIR)/%.o: $(SRC_DIR)/%.c
	$(CC) $(FLAGS) -c -o $@ $<

server: $(OBJS) $(SRC_DIR)/server.c
	$(CC) $(FLAGS) $(SRC_DIR)/server.c -o server $(OBJS)

client: $(OBJS) $(SRC_DIR)/client.c
	$(CC) $(FLAGS) $(SRC_DIR)/client.c -o client $(OBJS)

exp1_server: $(OBJS) $(SRC_DIR)/exp1_server.c
	$(CC) $(FLAGS) $(SRC_DIR)/exp1_server.c -o exp1_server $(OBJS)

exp1_client: $(OBJS) $(SRC_DIR)/exp1_client.c
	$(CC) $(FLAGS) $(SRC_DIR)/exp1_client.c -o exp1_client $(OBJS)

tests/testing_server: $(OBJS)
	$(CC) $(FLAGS) tests/testing_server.c -o tests/testing_server $(OBJS)

format:
	pre-commit run --all-files

test:
	sudo -E python3 tests/test_cp1.py
	sudo -E python3 tests/test_cp1_basic_ack_packets.py

clean:
	rm -f $(BUILD_DIR)/*.o peer client server
	rm -f tests/testing_server
