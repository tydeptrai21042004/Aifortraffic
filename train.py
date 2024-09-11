from __future__ import absolute_import
from __future__ import print_function
import os
import sys
import time
import optparse
import random
import serial
import numpy as np
import torch
import torch.optim as optim
import torch.nn.functional as F
import torch.nn as nn
import matplotlib.pyplot as plt
from sumolib import checkBinary  
import traci  
#import and setup 
if "SUMO_HOME" in os.environ:
    tools = os.path.join(os.environ["SUMO_HOME"], "tools")
    sys.path.append(tools)
else:
    sys.exit("please declare environment variable 'SUMO_HOME'")
#
#get_vehicle_numbers(lanes): This function calculates how many vehicles are in each lane, specifically checking vehicles that are more than 10 units from the start of the lane.
#get_waiting_time(lanes): This calculates the total waiting time for all vehicles in the given lanes.
#phaseDuration(junction, phase_time, phase_state): Sets the traffic light's phase (e.g., red, yellow, green) and its duration for a specific junction.
#pad_state(state, input_dims): Ensures that the input state (number of vehicles per lane) matches the expected dimensions by either padding with zeros or trimming excess values.
def get_vehicle_numbers(lanes):
    vehicle_per_lane = dict()
    for l in lanes:
        vehicle_per_lane[l] = 0
        for k in traci.lane.getLastStepVehicleIDs(l):
            if traci.vehicle.getLanePosition(k) > 10:
                vehicle_per_lane[l] += 1
    return vehicle_per_lane


def get_waiting_time(lanes):
    waiting_time = 0
    for lane in lanes:
        waiting_time += traci.lane.getWaitingTime(lane)
    return waiting_time


def phaseDuration(junction, phase_time, phase_state):
    traci.trafficlight.setRedYellowGreenState(junction, phase_state)
    traci.trafficlight.setPhaseDuration(junction, phase_time)
def pad_state(state, input_dims):
    # If the state is smaller than input_dims, pad with zeros
    if len(state) < input_dims:
        padded_state = np.zeros(input_dims)
        padded_state[:len(state)] = state
    # If the state is larger than input_dims, trim it
    elif len(state) > input_dims:
        padded_state = state[:input_dims]
    else:
        padded_state = state
#Model class: This defines a neural network with 3 layers using PyTorch. It takes inputs, processes them through two fully connected layers (fc1 and fc2), and outputs action values.
#Forward pass: The model uses ReLU activations and outputs actions for controlling traffic lights at a junction.
#Optimizer and loss function: The model uses the Adam optimizer and Mean Squared Error (MSE) loss to update the network during training.
class Model(nn.Module):
    def __init__(self, lr, input_dims, fc1_dims, fc2_dims, n_actions):
        super(Model, self).__init__()
        self.lr = lr
        self.input_dims = input_dims
        self.fc1_dims = fc1_dims
        self.fc2_dims = fc2_dims
        self.n_actions = n_actions
        self.linear1 = nn.Linear(self.input_dims, self.fc1_dims)
        self.linear2 = nn.Linear(self.fc1_dims, self.fc2_dims)
        self.linear3 = nn.Linear(self.fc2_dims, self.n_actions)
        self.optimizer = optim.Adam(self.parameters(), lr=self.lr)
        self.loss = nn.MSELoss()
        self.device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
        self.to(self.device)
    def forward(self, state):
        x = F.relu(self.linear1(state))
        x = F.relu(self.linear2(x))
        actions = self.linear3(x)
        return actions
#Agent class: This defines the reinforcement learning agent that interacts with the traffic system. Key attributes include:
#Gamma (γ): Discount factor for future rewards.
#Epsilon (ε): Exploration factor, determining how often the agent chooses a random action (to explore) versus selecting the best-known action (to exploit).
#Memory: A replay memory to store past experiences for each junction.
#Action space: The number of possible actions the agent can take (e.g., which phase to set for the traffic lights).
#store_transition(): Saves the agent's state, action, reward, and next state after each interaction with the environment. This helps the agent learn from past experiences.
#choose_action(): Chooses an action based on the current state. If a random number is greater than epsilon, the agent selects the action with the highest predicted reward; otherwise, it explores by selecting a random action.
#learn(): Updates the agent's neural network by calculating the loss between the predicted and target Q-values (expected future rewards).
#Q-learning: The agent uses Q-learning, where it learns a policy (what actions to take) by approximating Q-values using a neural network
#training online như em đã nói không muốn thì chỉnh nó thành offline thì nó sẽ chỉ load model đã train a file pre
class Agent:
    def __init__(
        self,
        gamma,
        epsilon,
        lr,
        input_dims,
        fc1_dims,
        fc2_dims,
        batch_size,
        n_actions,
        junctions,
        max_memory_size=100000,
        epsilon_dec=5e-4,
        epsilon_end=0.05,
    ):
        self.gamma = gamma
        self.epsilon = epsilon
        self.lr = lr
        self.batch_size = batch_size
        self.input_dims = input_dims
        self.fc1_dims = fc1_dims
        self.fc2_dims = fc2_dims
        self.n_actions = n_actions
        self.action_space = [i for i in range(n_actions)]
        self.junctions = junctions
        self.max_mem = max_memory_size
        self.epsilon_dec = epsilon_dec
        self.epsilon_end = epsilon_end
        self.mem_cntr = 0
        self.iter_cntr = 0
        self.replace_target = 100

        self.Q_eval = Model(
            self.lr, self.input_dims, self.fc1_dims, self.fc2_dims, self.n_actions
        )
        self.memory = dict()
        for junction in junctions:
            self.memory[junction] = {
                "state_memory": np.zeros(
                    (self.max_mem, self.input_dims), dtype=np.float32
                ),
                "new_state_memory": np.zeros(
                    (self.max_mem, self.input_dims), dtype=np.float32
                ),
                "reward_memory":np.zeros(self.max_mem, dtype=np.float32),
                "action_memory": np.zeros(self.max_mem, dtype=np.int32),
                "terminal_memory": np.zeros(self.max_mem, dtype=np.bool_),
                "mem_cntr": 0,
                "iter_cntr": 0,
            }
    def store_transition(self, state, state_, action, reward, done, junction):
        # Check if input dimensions match the expected dimensions
        if len(state) != self.input_dims or len(state_) != self.input_dims:
            #print(f"Skipping transition for junction {junction} due to mismatched input dims")
            return  # Skip if dimensions do not match

        state = pad_state(state, self.input_dims)
        state_ = pad_state(state_, self.input_dims)        
        index = self.memory[junction]["mem_cntr"] % self.max_mem

        self.memory[junction]["state_memory"][index] = state
        self.memory[junction]["new_state_memory"][index] = state_
        self.memory[junction]['reward_memory'][index] = reward
        self.memory[junction]['terminal_memory'][index] = done
        self.memory[junction]["action_memory"][index] = action
        self.memory[junction]["mem_cntr"] += 1


    def choose_action(self, observation):
        # Check if input dimensions match the expected dimensions
        if len(observation) != self.input_dims:
            #print("Skipping action selection due to mismatched input dims")
            return np.random.choice(self.action_space)  # Return a random action as fallback

        state = torch.tensor([observation], dtype=torch.float).to(self.Q_eval.device)
        if np.random.random() > self.epsilon:
            actions = self.Q_eval.forward(state)
            action = torch.argmax(actions).item()
        else:
            action = np.random.choice(self.action_space)
        return action

    
    def reset(self,junction_numbers):
        for junction_number in junction_numbers:
            self.memory[junction_number]['mem_cntr'] = 0

    def save(self,model_name):
        torch.save(self.Q_eval.state_dict(),f'models/{model_name}.bin')

    def learn(self, junction):
        self.Q_eval.optimizer.zero_grad()

        batch= np.arange(self.memory[junction]['mem_cntr'], dtype=np.int32)

        state_batch = torch.tensor(self.memory[junction]["state_memory"][batch]).to(
            self.Q_eval.device
        )
        new_state_batch = torch.tensor(
            self.memory[junction]["new_state_memory"][batch]
        ).to(self.Q_eval.device)
        reward_batch = torch.tensor(
            self.memory[junction]['reward_memory'][batch]).to(self.Q_eval.device)
        terminal_batch = torch.tensor(self.memory[junction]['terminal_memory'][batch]).to(self.Q_eval.device)
        action_batch = self.memory[junction]["action_memory"][batch]

        q_eval = self.Q_eval.forward(state_batch)[batch, action_batch]
        q_next = self.Q_eval.forward(new_state_batch)
        q_next[terminal_batch] = 0.0
        q_target = reward_batch + self.gamma * torch.max(q_next, dim=1)[0]
        loss = self.Q_eval.loss(q_target, q_eval).to(self.Q_eval.device)

        loss.backward()
        self.Q_eval.optimizer.step()

        self.iter_cntr += 1
        self.epsilon = (
            self.epsilon - self.epsilon_dec
            if self.epsilon > self.epsilon_end
            else self.epsilon_end
        )


def run(train=True, online=True, model_name="model", epochs=5, steps=500):
    """Execute the TraCI control loop"""
    epochs = epochs
    steps = steps
    best_time = np.inf
    total_waiting_time_list = list()
    total_travel_time_list = list()
    total_queue_length_list = list()
    
    # Load the model
    traci.start(
        [checkBinary("sumo"), "-c", "configuration.sumocfg", "--tripinfo-output", "maps/tripinfo.xml"]
    )
    all_junctions = traci.trafficlight.getIDList()
    junction_numbers = list(range(len(all_junctions)))

    brain = Agent(
        gamma=0.99,
        epsilon=0.0,
        lr=0.1,
        input_dims=4,
        fc1_dims=256,
        fc2_dims=256,
        batch_size=1024,
        n_actions=4,
        junctions=junction_numbers,
    )

    # Load existing model
    brain.Q_eval.load_state_dict(torch.load(f'models/{model_name}.bin', map_location=brain.Q_eval.device,weights_only=True))
    print(f"Model loaded on device: {brain.Q_eval.device}")
    
    traci.close()

    if train and online:
        print("Continuing online training...")
    elif train and not online:
        print("Offline mode: Running without training...")

    for e in range(epochs):
        total_travel_time = 0  
        total_queue_length = 0  

        # Start SUMO with different modes depending on online/offline
        if train and online:
            traci.start([checkBinary("sumo"), "-c", "configuration.sumocfg", "--tripinfo-output", "tripinfo.xml"])
        else:
            traci.start([checkBinary("sumo-gui"), "-c", "configuration.sumocfg", "--tripinfo-output", "tripinfo.xml"])

        print(f"Epoch: {e}")
        select_lane = [
            ["yyyrrrrrrrrr", "GGGrrrrrrrrr"],
            ["rrryyyrrrrrr", "rrrGGGrrrrrr"],
            ["rrrrrryyyrrr", "rrrrrrGGGrrr"],
            ["rrrrrrrrryyy", "rrrrrrrrrGGG"],
        ]
        step = 0
        total_waiting_time = 0
        min_duration = 5

        traffic_lights_time = dict()
        prev_wait_time = dict()
        prev_vehicles_per_lane = dict()
        prev_action = dict()
        all_lanes = list()

        for junction_number, junction in enumerate(all_junctions):
            prev_wait_time[junction] = 0
            prev_action[junction_number] = 0
            traffic_lights_time[junction] = 0
            prev_vehicles_per_lane[junction_number] = [0] * 4
            all_lanes.extend(list(traci.trafficlight.getControlledLanes(junction)))

        while step <= steps:
            traci.simulationStep()
            for junction_number, junction in enumerate(all_junctions):
                controlled_lanes = traci.trafficlight.getControlledLanes(junction)
                waiting_time = get_waiting_time(controlled_lanes)
                total_waiting_time += waiting_time

                queue_length = sum([traci.lane.getLastStepHaltingNumber(lane) for lane in controlled_lanes])
                total_queue_length += queue_length

                vehicle_ids = traci.vehicle.getIDList()
                for vehicle_id in vehicle_ids:
                    travel_time = traci.vehicle.getAccumulatedWaitingTime(vehicle_id)
                    total_travel_time += travel_time

                if traffic_lights_time[junction] == 0:
                    vehicles_per_lane = get_vehicle_numbers(controlled_lanes)
                    reward = -1 * waiting_time
                    state_ = list(vehicles_per_lane.values())
                    state = prev_vehicles_per_lane[junction_number]
                    prev_vehicles_per_lane[junction_number] = state_

                    # Store transitions only in online mode
                    if train and online:
                        brain.store_transition(state, state_, prev_action[junction_number], reward, (step == steps), junction_number)

                    lane = brain.choose_action(state_)
                    prev_action[junction_number] = lane
                    phaseDuration(junction, 6, select_lane[lane][0])
                    phaseDuration(junction, min_duration + 10, select_lane[lane][1])

                    traffic_lights_time[junction] = min_duration + 10
                    if train and online:
                        brain.learn(junction_number)
                else:
                    traffic_lights_time[junction] -= 1
            step += 1

        print("Total waiting time:", total_waiting_time)
        total_waiting_time_list.append(total_waiting_time)
        total_travel_time_list.append(total_travel_time)
        total_queue_length_list.append(total_queue_length)
        print(f"Total travel time of all vehicles: {total_travel_time}")
        print(f"Total queue length: {total_queue_length}")

        if total_waiting_time < best_time:
            best_time = total_waiting_time
            if train and online:
                brain.save(model_name)

        traci.close()

        if not train or not online:
            break  # Stop after first epoch in offline mode

    # Plot results after training
    if train:
        plt.plot(list(range(len(total_waiting_time_list))), total_waiting_time_list)
        plt.xlabel("Epochs")
        plt.ylabel("Total waiting time")
        plt.savefig(f'plots/time_vs_epoch_{model_name}.png')
        plt.show()

        plt.plot(list(range(len(total_travel_time_list))), total_travel_time_list)
        plt.xlabel("Epochs")
        plt.ylabel("Total travel time")
        plt.savefig(f'plots/travel_time_vs_epoch_{model_name}.png')
        plt.show()

        plt.plot(list(range(len(total_queue_length_list))), total_queue_length_list)
        plt.xlabel("Epochs")
        plt.ylabel("Total queue length")
        plt.savefig(f'plots/queue_length_vs_epoch_{model_name}.png')
        plt.show()



def get_options():
    optParser = optparse.OptionParser()
    optParser.add_option(
        "-m",
        dest='model_name',
        type='string',
        default="model",
        help="Name of model",
    )
    optParser.add_option(
        "--train",
        action='store_true',
        default=False,
        help="Training or testing mode",
    )
    optParser.add_option(
        "--online",
        action='store_true',
        default=True,
        help="Online training (continue training with a new map)",
    )
    optParser.add_option(
        "-e",
        dest='epochs',
        type='int',
        default=50,
        help="Number of epochs",
    )
    optParser.add_option(
        "-s",
        dest='steps',
        type='int',
        default=500,
        help="Number of steps",
    )

    options, args = optParser.parse_args()
    return options

if __name__ == "__main__":
    options = get_options()
    model_name = options.model_name
    train = options.train
    online = options.online
    epochs = options.epochs
    steps = options.steps

    run(train=train, online=online, model_name=model_name, epochs=epochs, steps=steps)
