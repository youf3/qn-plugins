import yaml
import TimeTagger
import numpy as np
import time

class TimeTaggerManager:
    def __init__(self, filename=None):
        if filename != None:
            # Store the provided TimeTagger instance
            #self.Inst = TimeTagger.createTimeTaggerNetwork('10.0.0.50')  # TimeTagger instance stored in self.Inst
            self.Inst = TimeTagger.createTimeTagger()  # TimeTagger instance stored in self.Inst
            print("Time tagger is connected")
        else:
            self.Inst = None
            print("Time tagger does not exist")
            return

        # Initialize the channel parameters at the class level
        self.Chlist = []
        self.TriggerLevels = []
        self.Deadtimes = []
        self.DelayTimes = []
        self.DataAcquisitionTime = None

        # Load the timetagger configuration
        self.load_timetagger_config(filename)

    def load_timetagger_config(self, filename):
        # Load the data from the YAML file
        with open(filename, 'r') as file:
            config = yaml.safe_load(file)

        # Load channel data directly into the class-level attributes
        for channel, data in config['TimeTagger']['Channels'].items():
            self.Chlist.append(data['ChannelID'])
            self.TriggerLevels.append(data['TriggerLevel'])
            self.Deadtimes.append(data['Deadtime'])
            self.DelayTimes.append(data['DelayTime'])

        # Load Data Acquisition Time
        self.DataAcquisitionTime = config['TimeTagger']['DataAcquisitionTime']
        print("TimetaggerConfig file is successfully loaded and initialized")

    def initTTChs(self):
        try:
            # Ensure all lists have the same length
            if not (len(self.Chlist) == len(self.TriggerLevels) == len(self.Deadtimes) == len(self.DelayTimes)):
                raise ValueError("Mismatch in length of Chlist, TriggerLevels, Deadtimes, or DelayTimes")

            # Print header
            header_format = '{:<10} {:<18} {:<18} {:<15}'
            print(header_format.format('Channel', 'TriggerLevel (V)', 'DelayTime (ps)', 'DeadTime (ps)'))
            print('-' * 65)

            # Initialize channels
            for channel, trigger_level, deadtime, delay_time in zip(self.Chlist, self.TriggerLevels, self.Deadtimes, self.DelayTimes):
                self.Inst.setTriggerLevel(channel, trigger_level)
                self.Inst.setDeadtime(channel, deadtime)
                self.Inst.setDelaySoftware(channel, delay_time * 1e12)  # Convert to ps
                self.Inst.setTestSignal(int(channel), False)

                row_format = '{:<10} {:<15.2f} {:<15.2f} {:<15.2f}'
                print(row_format.format(channel, trigger_level, delay_time * 1e12, deadtime))

            print('-' * 65)
        except ValueError as ve:
            print(f"Validation error: {ve}")
        except Exception as e:
            print(f"Error occurred while initiating TimTaggerManager channels: {e}")

    def TTChangeParams(self, TTCh, param, value):
        """Modify parameters like DelaySoftware, TriggerLevel, Deadtime for a specific channel (TTCh), update the corresponding list, and print updated parameters."""
        try:
            # Check if the TTCh exists in Chlist
            if TTCh not in self.Chlist:
                raise ValueError(f"Channel {TTCh} not found in Chlist.")

            # Find the index of the channel in Chlist
            index = self.Chlist.index(TTCh)

            # Use match-case for different parameters
            # match param:
            #     case "DelaySoftware":
            #         self.Inst.setDelaySoftware(TTCh, value * 1e12)  # Convert to ps
            #         self.DelayTimes[index] = value  # Update the DelayTime directly in the list
            #         print(f"Set DelaySoftware for channel {TTCh} to {value} ps")
                
            #     case "TriggerLevel":
            #         self.Inst.setTriggerLevel(TTCh, value)
            #         self.TriggerLevels[index] = value  # Update the TriggerLevel directly in the list
            #         print(f"Set TriggerLevel for channel {TTCh} to {value} V")
                
            #     case "Deadtime":
            #         self.Inst.setDeadtime(TTCh, value)
            #         self.Deadtimes[index] = value  # Update the Deadtime directly in the list
            #         print(f"Set Deadtime for channel {TTCh} to {value} ps")
                
            #     case _:
            #         raise ValueError(f"Invalid parameter {param}. Valid parameters are: DelaySoftware, TriggerLevel, Deadtime.")

            if param == "DelaySoftware":
                self.Inst.setDelaySoftware(TTCh, value * 1e12)  # Convert to ps
                self.DelayTimes[index] = value  # Update the DelayTime directly in the list
                print(f"Set DelaySoftware for channel {TTCh} to {value} ps")

            elif param == "TriggerLevel":
                self.Inst.setTriggerLevel(TTCh, value)
                self.TriggerLevels[index] = value  # Update the TriggerLevel directly in the list
                print(f"Set TriggerLevel for channel {TTCh} to {value} V")

            elif param == "Deadtime":
                self.Inst.setDeadtime(TTCh, value)
                self.Deadtimes[index] = value  # Update the Deadtime directly in the list
                print(f"Set Deadtime for channel {TTCh} to {value} ps")

            else:
                raise ValueError(f"Invalid parameter {param}. Valid parameters are: DelaySoftware, TriggerLevel, Deadtime.")
            
            # Print the updated parameters for all channels
            print("\nUpdated Channel Parameters:")
            header_format = '{:<10} {:<18} {:<18} {:<15}'
            print(header_format.format('Channel', 'TriggerLevel (V)', 'DelayTime (ps)', 'DeadTime (ps)'))
            print('-' * 65)

            # Retrieve and print the current values for all channels from the lists
            for i, channel in enumerate(self.Chlist):
                trigger_level = self.TriggerLevels[i]
                delay_time = self.DelayTimes[i]
                deadtime = self.Deadtimes[i]
                row_format = '{:<10} {:<15.2f} {:<15.2f} {:<15.2f}'
                print(row_format.format(channel, trigger_level, delay_time, deadtime))

            print('-' * 65)

        except Exception as e:
            print(f"Error while changing parameter {param} for channel {TTCh}: {e}")

    def enableTestSignals(self, Chlist=None):
        """Enable test signals on each channel for testing using the Chlist."""
        try:
            # Enable test signals for each channel in Chlist
            for ch in Chlist:
                self.Inst.setTestSignal(ch, True)
                print(f"Test signal enabled for channels in {ch}.")
        except Exception as e:
            print(f"Error enabling test signals: {e}")

    def disableTestSignals(self, Chlist=None):
        """Disable test signals on each channel after testing using the Chlist."""
        try:
            # Disable test signals for each channel in Chlist
            for ch in Chlist:
                self.Inst.setTestSignal(ch, False)
            print("Test signals disabled for all channels in Chlist.")
        except Exception as e:
            print(f"Error disabling test signals: {e}")

    def TTSyncMeasure(self, filenameWrite, Chlist):
        """Handles synchronized measurement and writes the data to a file."""
        try:
            if filenameWrite is None:
                raise ValueError("Filename for writing the data is not provided.")

            if not Chlist:
                raise ValueError("Channel list (Chlist) is not provided.")

            # Synchronized measurement
            synchronized = TimeTagger.SynchronizedMeasurements(self.Inst)
            print(f"Starting synchronized measurement for {self.DataAcquisitionTime} s")
            start_time = time.perf_counter()

            # File writing setup using the provided Chlist
            filewriter = TimeTagger.FileWriter(synchronized.getTagger(), filenameWrite, Chlist)
            synchronized.startFor(int(self.DataAcquisitionTime)*1e12)  # Use the configured acquisition time
            synchronized.waitUntilFinished()

            print(f"time taken for measurement:{time.perf_counter()-start_time} s")
            print(f"Data written to {filenameWrite}")
        except Exception as e:
            print(f"Error during synchronized measurement: {e}")
    
    def getChannelCounts(self, Chlist, measurement_time=1, printout=False):
        """Count the number of events on the specified channels."""
        try:
            # Create a Counter to count events on the specified channels
            counter = TimeTagger.Counter(self.Inst, Chlist, binwidth=measurement_time*1e12)

            # Start the measurement for a given time (in seconds)
            counter.startFor(int(measurement_time*1e12))  # Measurement time in picoseconds
            counter.waitUntilFinished()

            # Get the counts for each channel
            counts = counter.getData()

            if printout:
                header_format = '{:<10} {:<18}'
                print(header_format.format('Channel', 'Counts'))
                print('-' * 65)

                # Print the counts
                for i, ch in enumerate(Chlist):
                    row_format = '{:<10} {:<18}'
                    print(row_format.format(ch, float(counts[i])))
                print('-' * 65)

        except Exception as e:
            print(f"Error while counting events on channels: {e}")
        
        return counts
    

    def getChannelCountRate(self, Chlist, measurement_time=1, printout=False):
        """Get the count rate (events per second) on the specified channels."""
        try:
            # Create a Counter to count events on the specified channels
            count_rates = TimeTagger.Countrate(self.Inst, Chlist)

            # Start the measurement for a given time (in seconds)
            count_rates.startFor(int(measurement_time * 1e12))  # Measurement time in picoseconds
            count_rates.waitUntilFinished()

            # Get the counts for each channel
            countrates = count_rates.getData()

            if printout:
                # Print the count rates
                header_format = '{:<10} {:<18}'
                print(header_format.format('Channel', 'Count rate (/s)'))
                print('-' * 65)

                # Print the counts
                for i, ch in enumerate(Chlist):
                    row_format = '{:<10} {:<18}'
                    print(row_format.format(ch, float(countrates[i])))
                print('-' * 65)
            return countrates

        except Exception as e:
            print(f"Error while calculating count rates on channels: {e}")
            return None
        
        return count_rates
    
    def getCoincidences(self, channel_pairs, measurement_time=1, bindwidth=1e3, n_bins=100):
        """Compute coincidences for specified channel pairs."""
        # coincidences = []
        # for ch1, ch2 in channel_pairs:
        #     correlation = TimeTagger.Correlation(self.Inst, ch1, ch2, binwidth=bindwidth, n_bins=n_bins)
        #     correlation.startFor(int(measurement_time * 1e12))
        #     correlation.waitUntilFinished()
        #     coincidences.append(correlation.getData())
        # return coincidences
        correlations = []
        
        # Initialize all correlation measurements
        for ch1, ch2 in channel_pairs:
            correlation = TimeTagger.Correlation(self.Inst, ch1, ch2, binwidth=bindwidth, n_bins=n_bins)
            correlations.append(correlation)
        
        # Start all measurements simultaneously
        for correlation in correlations:
            correlation.startFor(int(measurement_time * 1e12))
        
        # Wait for all to finish
        for correlation in correlations:
            correlation.waitUntilFinished()
        
        # Collect results
        coincidences = [correlation.getData() for correlation in correlations]
        
        return coincidences


    def npSaveData(self, filenameRead=None, ShowDataTable = False):
        """Reads data from a file and saves it as a NumPy array."""
        try:
            if filenameRead is None:
                raise ValueError("Filenameread is not provided.")

            if ShowDataTable is False:
                print(f"!!!!!! Not printing the DataTable. If you want to print make DataTable=True !!!!!!!\n")

            # Initialize the file reader
            filereader = TimeTagger.FileReader(filenameRead)


            format_string = '{:>8} | {:>17} | {:>7} | {:>14} | {:>13}'
            print(format_string.format('TAG #', 'EVENT TYPE', 'CHANNEL', 'TIMESTAMP (ps)', 'MISSED EVENTS'))
            print('---------+-------------------+---------+----------------+--------------')

            n_events = 1000  # Number of events to read at once
            event_name = ['0 (TimeTag)', '1 (Error)', '2 (OverflowBegin)', '3 (OverflowEnd)', '4 (MissedEvents)']

            tempChannel = []
            tempTimestamp = []
            i = 0

            while filereader.hasData():
                # Get data in chunks
                data = filereader.getData(n_events=n_events)

                # Retrieve channels and timestamps
                channel = data.getChannels()
                timestamps = data.getTimestamps()
                overflow_types = data.getEventTypes()
                missed_events = data.getMissedEvents()  # The numbers of missed events in case of overflow
                OnlyTimeTags = np.squeeze(np.where(overflow_types == 0))

                tempChannel.append(channel[OnlyTimeTags])
                tempTimestamp.append(timestamps[OnlyTimeTags])
                
                
                # Output to table
                if ShowDataTable==True:
                    OnlyTimeTags = np.squeeze(np.where(overflow_types==0))

                    if i < 2 or not filereader.hasData():
                        print(format_string.format(*" "*5))
                        heading = ' Start of data chunk {} with {} events '.format(i+1, data.size)
                        extra_width = 69 - len(heading)
                        print('{} {} {}'.format("="*(extra_width//2), heading, "="*(extra_width - extra_width//2)))
                        print(format_string.format(*" "*5))
                        print(format_string.format(i*n_events + 1, event_name[overflow_types[0]], channel[0], timestamps[0], missed_events[0]))
                        if data.size > 1:
                            print(format_string.format(i*n_events + 2, event_name[overflow_types[1]], channel[1], timestamps[1], missed_events[1]))
                        if data.size > 3:
                            print(format_string.format(*["..."]*5))
                        if data.size > 2:
                            print(format_string.format(i*n_events + data.size, event_name[overflow_types[-1]], channel[-1], timestamps[-1], missed_events[-1]))
                    if i == 1:
                        print(format_string.format(*" "*5))
                        for j in range(3):
                            print(format_string.format(*"."*5))

                i += 1

                # pass

                # i += 1

            # Concatenate the results into a single array
            tempChannel = np.concatenate(tempChannel)
            tempTimestamp = np.concatenate(tempTimestamp)

            # Save the data to a NumPy array
            timeTagData = np.empty([tempChannel.size, 2])
            timeTagData[:, 0] = tempChannel
            timeTagData[:, 1] = tempTimestamp
            np.save(filenameRead.replace('.ttbin', '.npy'), timeTagData)

            print(f"Data saved to {filenameRead.replace('.ttbin', '.npy')}")

        except Exception as e:
            print(f"Error reading file: {e}")

    def close(self):
        """Close the TimeTagger instance."""
        try:
            if self.Inst is not None:
                TimeTagger.freeTimeTagger(self.Inst)
                self.Inst.close()
                print("TimeTagger instance closed successfully.")
            else:
                print("TimeTagger instance is not initialized.")
        except Exception as e:
            print(f"Error closing TimeTagger instance: {e}")

if __name__ == "__main__":
    TTU = TimeTaggerManager(filename="TimeTaggerConfig.yaml")
    TTU.getChannelCountRate(TTU.Chlist, printout= True)
    TimeTagger.freeTimeTagger(TTU.Inst)

    # while True:
    #     print('='*50)
    #     count_list=TTU.getChannelCounts(Chlist=TTU.Chlist, measurement_time=1, printout=False)[:,0]
    #     print(f"Total counts:{sum(count_list[0:3])}")
    #     counts1=int(count_list[0])
    #     counts3=int(count_list[2])
    #     coincidence_hist = TTU.getCoincidences(((1,3),),measurement_time=1, bindwidth=1e3, n_bins=200) # binwidth in ps (check this)
    #     print(f'1&3 Coin counts {np.sum(coincidence_hist)}')
    #     print(np.sum(coincidence_hist) / (counts1*counts3))
    #     print(np.max(coincidence_hist) / (counts1*counts3))

    # meas_times = [0.01,]
    # for _ in range(1):
    #     for meas_time in meas_times:
    #         tic=time.perf_counter()
    #         TTU.getChannelCounts(Chlist=TTU.Chlist, measurement_time=meas_time)
    #         # print(f"meas time: {meas_time}s, time taken:{time.perf_counter()-tic}s")